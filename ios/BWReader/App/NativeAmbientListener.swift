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
    @Published private(set) var heardSpeakers: [NativeAmbientPipeline.HeardSpeaker] = []
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
        let pipeline = NativeAmbientPipeline(locale: locale, source: source) { [weak self] event in
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
        case .heard(let list):
            heardSpeakers = list
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

    // MARK: 熟人

    func namePerson(_ speaker: NativeAmbientPipeline.HeardSpeaker, name: String) async -> String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != "我", !trimmed.hasPrefix("说话人") else { return "名字不能为空、不能叫「我」或「说话人…」" }
        guard let pipeline else { return "旁听没在运行，这段声音已经没有了" }
        return await withCheckedContinuation { continuation in
            pipeline.namePerson(speaker, name: String(trimmed.prefix(20))) { continuation.resume(returning: $0) }
        }
    }

    func peopleChanged() { pipeline?.reloadPeople() }

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
            if let names = reply["names"] as? [String: Any], !names.isEmpty { pipeline?.applyServerNames(names) }
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

/// 只被 NativeAmbientListener 持有。音频回调（tap）只记时钟、把样本转投 `work` 串行队列；
/// 分离、切段、分窗、存盘都在 `work` 上，所有可变状态只在 `work` 上改。
///
/// 转写是**两条线**（2026-09-26 实测后改）：
///   - 主线：「我的语言」的连续识别流一直开着，覆盖所有声音，词段按时间交分离器认人。便宜、不积压。
///   - 补线：按分离器给出的每人起止切成「一人一段」交 NativeSegmentTranscriber，**只**用于
///     ① 说别的语言的人（人物页登记的 / 推测出来的）→ 用他的语言重转，替换主线在这段的字；
///     ② 还不知道语言的人 → 取他最先几段试推测（不是每段都推测）。
///   纯逐段转写在电影这种人声不断的场景下转写速度远跟不上（实测只剩零星两句），所以不能当主线；
///   补线也不抢实时：段落连同音频落盘排队（NativeAmbientBackfill），周围安静时后台一段段转，
///   转完修正记录 —— 窗口还在本机就地替换，已送服务器的走 /api/ambient/revise（用户：「逐段转写应该在空闲时后台处理」）。
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
        case heard([HeardSpeaker])
        case failed(String)
    }

    /// 最近听到、还没名字的人：给设置页「给他起名」用。`ranges` 是他在管线时间轴上的说话区间。
    struct HeardSpeaker: Identifiable {
        let id: String
        let label: String
        let sample: String
        let ranges: [ClosedRange<Double>]
        let heardAt: Date
        let slot: Int
        let session: Int
        let slotKey: String
    }

    struct Utterance {
        var speaker: Int?
        var text: String
        var start: Double
        var end: Double
        var locale: String? = nil
        var langConfirmed = true
        var fromStream = false
    }

    /// 待转写的一段：一个人从开始到结束（管线时间轴）。
    struct Turn {
        let speaker: Int?
        let start: Double
        let end: Double
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
        /// 每个声音块：{slotKey, personId?, vector?}（服务器据此记时间轴、给块归人、补声纹库）
        var speakers: [[String: Any]] = []

        var utteranceCount: Int { utterances.count }
        var lastLine: String {
            guard let last = utterances.last else { return "" }
            return "\(last["speaker"] as? String ?? "?")：\(last["text"] as? String ?? "")"
        }
        var payload: [String: Any] {
            ["windowId": id, "startedAt": Int(startedAt.timeIntervalSince1970 * 1000),
             "endedAt": Int(endedAt.timeIntervalSince1970 * 1000), "speakerCount": speakerCount,
             "locale": locale, "source": source, "utterances": utterances, "speakers": speakers]
        }
    }

    static let turnMergeGap = 0.8               // 秒：同一个人两段之间停得比这短，算同一段
    static let turnSettle = 0.6                 // 秒：定稿时间线越过段尾这么久，才认为他说完了
    static let turnMaxSeconds = 28.0            // 一段最长；太长的切开（识别器对长段更容易丢字）
    static let turnMinSeconds = 0.35
    static let turnPad = 0.15                   // 前后各多取一点，免得切掉首尾音
    static let silenceToFinalize = 1.2          // 秒：主线识别出文字后这么久没新字，就结束当前识别任务拿定稿
    static let maxRecognitionSeconds = 45.0
    static let assignDelay = 1.5                // 秒：等分离结果定稿再给主线词段认人
    static let maxGuessesPerSpeaker = 3         // 每个声音块最多拿几段来推测语言
    static let guessMinSeconds = 1.2            // 太短的段推测不准，不拿来推测
    static let langVotesToTrust = 3             // 同一个人推测出同一种语言这么多次，之后直接沿用
    // 积压控制（2026-09-26 实测：开着电影时陌生人声源源不断，每段都按候选语言各转一遍，转写远跟不上，
    // 而分窗又要等「全部转完」—— 结果一窗都没送出去，时间轴一条记录都没有）
    static let idleSeconds = 4.0                // 秒：主线这么久没新字 = 周围安静，后台开始逐段重转
    static let windowWaitLimit = 20.0           // 秒：窗口该送了但主线还有更早的定稿没认人，最多等这么久
    static let windowGap = 12.0                 // 秒：两句之间停这么久就切窗口
    static let windowMaxSeconds = 120.0
    static let windowMaxChars = 1500
    static let ringSeconds = 240.0
    static let diarizerRecycleSeconds = 20 * 60.0

    private let locale: String                  // 「我的语言」：主线识别用它；用户本人与不分人时也用它
    private let recognizer: SFSpeechRecognizer?
    private let recognitionQueue: OperationQueue = {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        queue.qualityOfService = .utility
        return queue
    }()
    private let source: Source
    private let emit: (Event) -> Void
    private let engine = AVAudioEngine()
    private let work = DispatchQueue(label: "space.bwicarus.reader.ambient", qos: .utility)
    private let tapLock = NSLock()
    private let gain = NativeAmbientGain()      // 送识别器前的自动增益（远处的声音太小会被判成没有语音）
    private let diarizerQueue = DispatchQueue(label: "space.bwicarus.reader.ambient.diarizer", qos: .utility)
    static let diarizerMaxLag = 8.0             // 秒：分离器积压超过这么久就从当前时刻重建（宁可少分人，不能拖住转写）
    private var request: SFSpeechAudioBufferRecognitionRequest?   // 主线识别的当前请求（tap 往里追加）
    private var liveFeed: AnyObject?            // 主线用新识别接口时的连续识别器（NativeLiveTranscriber，iOS 26+）
    private var liveActive = false              // work 上：主线在用新接口（不用按停顿换任务）
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
    private var transcribedUntil: [Int: Double] = [:]  // 每个槽位已送转写到哪一秒（分离器时间轴）
    private let backfill = NativeAmbientBackfill()
    private var backfillBusy = false
    private var startEpochMs = Date().timeIntervalSince1970 * 1000   // 管线时刻 0 对应的绝对时间
    private var lastGuessAt: [Int: Double] = [:]
    private var windowDueSince: Date?
    // 诊断计数：每 30 秒有变化就写一行日志（「没有记录」时一眼看出卡在哪一步）
    private var stats = (streamed: 0, cut: 0, done: 0, empty: 0, revised: 0, sent: 0)
    // 主线识别器自身的计数：任务数 / 中间结果 / 定稿 / 出错（区分「识别器没听到」和「听到了但后面丢了」）
    private var recStats = (tasks: 0, partials: 0, finals: 0, errors: 0, lastError: 0)
    private var workLastTick = 0.0, workMaxGap = 0.0   // 周期任务间隔：work 队列被堵住时这里会变大
    private var diarizerResets = 0
    private var lastStatsLine = ""
    private var lastStatsAt = Date.distantPast
    private var slotLangVotes: [Int: [String: Int]] = [:]
    private var guessCount: [Int: Int] = [:]
    private var personLanguage: [String: String] = [:]   // personId → 登记的语言（"" = 没登记）
    private var replaced: [(speaker: Int, start: Double, end: Double)] = []  // 已被补线重转、主线要让出的时段
    // 主线识别
    private var task: SFSpeechRecognitionTask?
    private var taskGeneration = 0
    private var taskStarts: [Int: Double] = [:]
    private var rotationClock = 0.0
    private var lastText = ""
    private var lastTextAt = 0.0
    private var heardTextInTask = false
    private var pendingFinals: [(ready: Double, segments: [(text: String, start: Double, end: Double)])] = []
    private var lastSpeakerCount = -1
    private var heard: [HeardSpeaker] = []
    // 声纹比对（熟人不占分离器槽位）：槽位 → 名字，按分离器会话隔离
    private var diarizerSession = 0
    private var sessionID = ""                  // 声音块编号 slotKey = "<sessionID>:<槽位>"，服务器按它记时间轴
    private var slotNames: [Int: String] = [:]
    private var slotPersonIds: [Int: String] = [:]
    private var slotVectors: [Int: [Float]] = [:]
    private var slotCheckedSpeech: [Int: Double] = [:]  // 上次比对时该槽位的定稿说话秒数
    private var slotChecks: [Int: Int] = [:]
    private var identifying = false
    private var lastIdentifyCheck = 0.0
    static let identifyEvery = 3.0              // 秒：多久看一次有没有该比对的槽位
    static let identifyRecheckSpeech = 10.0     // 秒：多说了这么久再比一次（前几秒样本可能不准）
    static let identifyMaxChecks = 3
    private var loggedDangerWriteFailure = false
    private var utterances: [Utterance] = []
    private var windowStartedAt = Date()
    private var dangerFile: AVAudioFile?
    private var dangerUntil = Date.distantPast
    private var tick: DispatchSourceTimer?
    private var loggedNoDiarizer = false

    init(locale: String, source: Source, emit: @escaping (Event) -> Void) {
        self.locale = locale
        self.source = source
        self.emit = emit
        recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale))
        recognizer?.queue = recognitionQueue
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
        startEpochMs = Date().timeIntervalSince1970 * 1000
        work.sync {
            resampler = NativeResampler16k(sourceRate: format.sampleRate)
            beginMainLine()
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
        startEpochMs = Date().timeIntervalSince1970 * 1000
        work.sync {
            resampler = NativeResampler16k(sourceRate: NativeAudioEngine.sampleRate)
            beginMainLine()
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
        let live = liveFeed
        liveFeed = nil
        tapLock.unlock()
        current?.endAudio()
        if #available(iOS 26.0, *), let live = live as? NativeLiveTranscriber {
            Task.detached { await live.finish() }   // 最后一句的定稿会在停下后回来，由 onFinal 当场认人送出
        }
        if source == .call {
            NativeAudioEngine.microphoneTap.set(nil)
        } else {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        tick?.cancel()
        tick = nil
        work.async {
            self.task?.cancel()
            self.task = nil
            // 手里还没认人的主线词段、还没切出去的最后一段都处理掉
            for pending in self.pendingFinals { self.assign(pending.segments) }
            self.pendingFinals = []
            self.cutTurns(final: true)
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
        let streamTime = Double(streamFrames) / sampleRate
        if let live = liveFeed {
            if #available(iOS 26.0, *), let live = live as? NativeLiveTranscriber {
                live.append(gain.process(buffer) ?? buffer, streamTime: streamTime)
            }
        } else if let request { request.append(gain.process(buffer) ?? buffer) }
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
        // 说话人分离：在自己的队列上跑。模型慢于实时时不能堵住 work（识别结果、周期任务都在 work 上）。
        if let diarizer {
            let pending = diarizer.enqueued(pcm16k.count)
            if Double(pending) / NativeStreamDiarizer.sampleRate > Self.diarizerMaxLag {
                diarizerResets += 1
                NativeAmbientLog.note("旁听：说话人分离跟不上（积压 \(Int(Double(pending) / NativeStreamDiarizer.sampleRate)) 秒），从当前时刻重建", level: "error")
                self.diarizer = nil
                transcribedUntil = [:]
                loadDiarizer()
                return
            }
            diarizerQueue.async { [weak self] in
                do {
                    guard try diarizer.feed(pcm16k) != nil else { return }
                    let speakers = diarizer.activeSpeakers(within: 15, minSpeech: 1).count
                    self?.work.async {
                        guard let self, self.diarizer === diarizer, speakers != self.lastSpeakerCount else { return }
                        self.lastSpeakerCount = speakers
                        self.emit(.speakers(speakers))
                    }
                } catch {
                    NativeAmbientLog.note("旁听：说话人分离出错 \(error.localizedDescription)", level: "error")
                }
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
                let people = NativeVoiceprint.people().count
                self?.work.async {
                    guard let self else { return }
                    self.diarizerLoading = false
                    do {
                        let diarizer = try NativeStreamDiarizer(models: models, voiceprint: voiceprint)
                        self.diarizer = diarizer
                        self.diarizerOrigin = self.ring.isEmpty ? 0 : self.ringStart + Double(self.ring.count) / NativeStreamDiarizer.sampleRate
                        self.diarizerSession += 1
                        self.sessionID = String(UUID().uuidString.prefix(8)).lowercased()
                        self.slotNames = [:]
                        self.slotPersonIds = [:]
                        self.slotVectors = [:]
                        self.slotCheckedSpeech = [:]
                        self.slotChecks = [:]
                        NativeAmbientLog.note("旁听：说话人分离已接上（\(voiceprint == nil ? "没有声纹，「我」靠比对也认不出" : "用声纹认出「我」")"
                            + "，\(people) 位熟人靠声纹特征比对）")
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
        if let name = slotNames[index] { return (name, name == "我") }
        return ("说话人\(index + 1)", false)
    }

    // MARK: 主线：「我的语言」连续识别

    /// iOS 26+ 且新接口支持这种语言 → SpeechTranscriber 一条流连续识别；否则旧接口按停顿切任务。
    private func beginMainLine() {
        guard #available(iOS 26.0, *) else { beginRecognitionTask(); return }
        let live = NativeLiveTranscriber()
        tapLock.lock(); liveFeed = live; tapLock.unlock()
        liveActive = true
        recStats.tasks += 1
        let locale = self.locale
        Task.detached(priority: .utility) { [weak self] in
            guard await NativeLiveTranscriber.supports(locale) else {
                self?.work.async { self?.fallBackToLegacy("新识别接口不支持「\(NativeSegmentTranscriber.displayName(locale))」") }
                return
            }
            do {
                try await live.start(locale: locale,
                    onVolatile: { text in self?.work.async { self?.liveVolatile(text) } },
                    onFinal: { segments in self?.work.async { self?.liveFinal(segments) } },
                    onError: { error in self?.work.async { self?.fallBackToLegacy("新识别接口出错 \(error.localizedDescription)") } })
                NativeAmbientLog.note("旁听：主线改用新识别接口（SpeechTranscriber，\(NativeSegmentTranscriber.displayName(locale))）")
            } catch {
                self?.work.async { self?.fallBackToLegacy("新识别接口启动失败 \(error.localizedDescription)") }
            }
        }
    }

    private func fallBackToLegacy(_ reason: String) {
        tapLock.lock(); let wasLive = liveFeed != nil; liveFeed = nil; tapLock.unlock()
        liveActive = false
        recStats.errors += 1
        NativeAmbientLog.note("旁听：\(reason)，主线退回旧识别接口", level: "error")
        if wasLive && isRunning { beginRecognitionTask() }
    }

    private func liveVolatile(_ text: String) {
        guard text != lastText else { return }
        lastText = text
        lastTextAt = now
        if !text.isEmpty { recStats.partials += 1; heardTextInTask = true }
        emit(.partial(text))
    }

    private func liveFinal(_ segments: [(text: String, start: Double, end: Double)]) {
        recStats.finals += 1
        lastTextAt = now
        lastText = ""
        if isRunning {
            pendingFinals.append((ready: now + Self.assignDelay, segments: segments))
        } else {
            assign(segments)
            flushWindow(force: true)
        }
    }

    // MARK: 主线（旧接口）：按停顿切任务拿定稿

    private func beginRecognitionTask() {
        guard let recognizer else {
            NativeAmbientLog.note("旁听：没有「\(NativeSegmentTranscriber.displayName(locale))」的识别器，主线不转写", level: "error")
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        taskGeneration += 1
        recStats.tasks += 1
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
                if !text.isEmpty { recStats.partials += 1 }
                heardTextInTask = heardTextInTask || !text.isEmpty
                emit(.partial(text))
            }
            if result.isFinal, let base = taskStarts[generation] {
                let segments = result.bestTranscription.segments.map {
                    (text: $0.substring, start: base + $0.timestamp, end: base + $0.timestamp + $0.duration)
                }
                if !segments.isEmpty {
                    recStats.finals += 1
                    pendingFinals.append((ready: now + Self.assignDelay, segments: segments))
                }
                taskStarts[generation] = nil
            }
        }
        if let error {
            taskStarts[generation] = nil
            let code = (error as NSError).code
            // 1110 = 没检测到语音、216/203 = 任务被取消 / 结束：旋转时的正常结局，不算故障
            let benign = [1110, 216, 203, 301].contains(code)
            if !benign {
                recStats.errors += 1; recStats.lastError = code
                NativeAmbientLog.note("旁听：主线识别出错 code=\(code) \(error.localizedDescription)", level: "error")
            }
            if generation == taskGeneration && isRunning {
                if benign { beginRecognitionTask() }
                else { emit(.failed("主线识别出错 code=\(code)")) }
            }
        }
    }

    /// 主线定稿词段 → 按分离结果认人 → 同一人相邻的合句。已被逐段重转替换的时段让出。
    private func assign(_ segments: [(text: String, start: Double, end: Double)]) {
        for segment in segments {
            let speaker = diarizer?.dominantSpeaker(from: segment.start - diarizerOrigin, to: segment.end - diarizerOrigin)
            let middle = (segment.start + segment.end) / 2
            if let speaker, replaced.contains(where: { $0.speaker == speaker && $0.start <= middle && middle <= $0.end }) {
                continue
            }
            if var last = utterances.last, last.fromStream, last.speaker == speaker, segment.start - last.end < 1.5 {
                last.text += segment.text
                last.end = segment.end
                utterances[utterances.count - 1] = last
            } else {
                utterances.append(Utterance(speaker: speaker, text: segment.text, start: segment.start, end: segment.end,
                                            locale: locale, langConfirmed: true, fromStream: true))
                stats.streamed += 1
            }
        }
    }

    // MARK: 周期（每 0.5 秒，在 work 上）

    private func periodic() {
        let t = now
        if workLastTick > 0 { workMaxGap = max(workMaxGap, t - workLastTick) }
        workLastTick = t
        if t - lastIdentifyCheck >= Self.identifyEvery {
            lastIdentifyCheck = t
            identifyNextSlot()
        }
        // 1) 主线：识别出文字后 1.2 秒没新字（或本任务满 45 秒）就换任务拿定稿；定稿满 1.5 秒的认人合句
        if !liveActive, heardTextInTask && t - lastTextAt >= Self.silenceToFinalize || !liveActive && t - rotationClock >= Self.maxRecognitionSeconds {
            if heardTextInTask { rotateRecognition() } else { rotationClock = t }
        }
        while let first = pendingFinals.first, first.ready <= t {
            pendingFinals.removeFirst()
            assign(first.segments)
        }
        // 2) 补线：按每个人的起止切段，说别的语言 / 待推测的人的段落盘排队；周围安静时后台逐段重转
        cutTurns(final: false)
        processBackfillIfIdle()
        logStats()
        // 2) 切窗口
        flushWindow(force: false)
        // 3) 危险录音到点
        if dangerFile != nil, Date() >= dangerUntil { closeDangerFile() }
        // 4) 分离器定期换新（时间线会一直长；只在窗口刚清空时换，免得同一窗口里编号变）
        if let diarizer, utterances.isEmpty, pendingFinals.isEmpty, !identifying, !backfillBusy,
           diarizer.elapsed > Self.diarizerRecycleSeconds {
            self.diarizer = nil
            transcribedUntil = [:]
            loadDiarizer()
        }
    }

    // MARK: 切段（一人一段）

    /// 分离器已定稿的片段 → 同一个人相邻的合并 → 定稿线越过段尾 0.6 秒（他说完了）才切出去送转写。
    /// `final`：停止旁听时，没说完的也切。
    private func cutTurns(final: Bool) {
        guard let diarizer else {
            if !loggedNoDiarizer {
                loggedNoDiarizer = true
                NativeAmbientLog.note("旁听：分离器还没就绪，先只用主线识别，不分人")
            }
            return
        }
        let settled = final ? .infinity : diarizer.finalizedUntil - Self.turnSettle
        var open: [Int: (start: Double, end: Double)] = [:]
        var turns: [Turn] = []
        func close(_ speaker: Int, _ span: (start: Double, end: Double)) {
            var start = span.start
            while span.end - start > Self.turnMaxSeconds {
                turns.append(Turn(speaker: speaker, start: start + diarizerOrigin, end: start + Self.turnMaxSeconds + diarizerOrigin))
                start += Self.turnMaxSeconds
            }
            turns.append(Turn(speaker: speaker, start: start + diarizerOrigin, end: span.end + diarizerOrigin))
            transcribedUntil[speaker] = span.end
        }
        for segment in diarizer.finalizedSegments() where segment.end > (transcribedUntil[segment.speaker] ?? -1) {
            let start = max(segment.start, transcribedUntil[segment.speaker] ?? -1)
            if let current = open[segment.speaker] {
                if start - current.end <= Self.turnMergeGap {
                    open[segment.speaker] = (current.start, max(current.end, segment.end))
                    continue
                }
                close(segment.speaker, current)
            }
            open[segment.speaker] = (start, segment.end)
        }
        for (speaker, span) in open where span.end <= settled { close(speaker, span) }
        for turn in turns.sorted(by: { $0.start < $1.start }) { enqueue(turn) }
    }

    // MARK: 转写（按这个人的语言；没登记就推测）

    /// 需要重转的段：音频落盘排队，不当场转（空闲时后台处理）。
    private func enqueue(_ turn: Turn) {
        guard turn.end - turn.start >= Self.turnMinSeconds, let speaker = turn.speaker else { return }
        // 主线已覆盖「我的语言」：只有说别的语言、或还要推测语言的人才重转
        guard let plan = segmentPlan(speaker: speaker, duration: turn.end - turn.start) else { return }
        let from = max(ringStart, turn.start - Self.turnPad)
        let to = min(ringStart + Double(ring.count) / NativeStreamDiarizer.sampleRate, turn.end + Self.turnPad)
        let a = Int((from - ringStart) * NativeStreamDiarizer.sampleRate)
        let b = Int((to - ringStart) * NativeStreamDiarizer.sampleRate)
        guard a >= 0, b > a, b <= ring.count else {
            NativeAmbientLog.note("旁听：一段声音已滚出缓冲，没能排进重转队列", level: "error")
            return
        }
        if plan.locale == nil {
            guessCount[speaker, default: 0] += 1
            lastGuessAt[speaker] = now
        }
        stats.cut += 1
        let job = NativeAmbientBackfill.Job(
            id: UUID().uuidString, slotKey: slotKey(speaker), speaker: speaker, session: sessionID,
            t0: epochMs(turn.start), t1: epochMs(turn.end), streamStart: turn.start, streamEnd: turn.end,
            locale: plan.locale, confirmed: plan.confirmed, attempts: 0)
        backfill.add(job, samples: Array(ring[a..<b]))
    }

    private func epochMs(_ streamTime: Double) -> Double { startEpochMs + streamTime * 1000 }

    /// 周围安静（主线 4 秒没新字、没有待认人的定稿）或旁听已停时，取一段重转。一次只转一段。
    private func processBackfillIfIdle() {
        guard !backfillBusy, let job = backfill.next() else { return }
        let quiet = now - max(lastTextAt, rotationClock) >= Self.idleSeconds && pendingFinals.isEmpty
        guard quiet || !isRunning else { return }
        guard let samples = backfill.samples(of: job) else { backfill.finish(job); return }
        backfillBusy = true
        Task.detached(priority: .background) { [weak self] in
            let result: NativeSegmentTranscriber.Result?
            if let locale = job.locale {
                result = await NativeSegmentTranscriber.shared.transcribe(samples, locale: locale)
            } else {
                result = await NativeSegmentTranscriber.shared.guess(samples)
            }
            self?.work.async { self?.applyBackfill(job, result: result) }
        }
    }

    /// 重转结果：推测投票；是我的语言就不改（主线已对）；否则替换这段 —— 窗口还在本机就地改，已送出就修服务器时间轴。
    private func applyBackfill(_ job: NativeAmbientBackfill.Job, result: NativeSegmentTranscriber.Result?) {
        backfillBusy = false
        guard let result, !result.text.trimmingCharacters(in: .whitespaces).isEmpty else {
            stats.empty += 1
            backfill.finish(job)
            return
        }
        stats.done += 1
        let current = job.session == sessionID
        let guessed = !result.scores.isEmpty
        if guessed {
            let scores = result.scores.map { "\(NativeSegmentTranscriber.displayName($0.key)) \($0.value)" }.sorted().joined(separator: "，")
            NativeAmbientLog.note("逐段重转：\(job.slotKey) 推测为\(NativeSegmentTranscriber.displayName(result.locale))（\(scores)）")
            if current { slotLangVotes[job.speaker, default: [:]][result.locale, default: 0] += 1 }
            if result.locale == locale { backfill.finish(job); return }   // 就是我的语言：主线已经转对了
        }
        let confirmed = job.confirmed && !guessed
        let local = current && utterances.contains { $0.speaker == job.speaker && $0.start < job.streamEnd + 0.3 && $0.end > job.streamStart - 0.3 }
        if local {
            replaced.append((job.speaker, job.streamStart - 0.3, job.streamEnd + 0.3))
            if replaced.count > 300 { replaced.removeFirst(replaced.count - 300) }
            utterances.removeAll { u in
                u.fromStream && u.speaker == job.speaker && u.start < job.streamEnd + 0.3 && u.end > job.streamStart - 0.3
            }
            utterances.append(Utterance(speaker: job.speaker, text: result.text, start: job.streamStart, end: job.streamEnd,
                                        locale: result.locale, langConfirmed: confirmed))
            stats.revised += 1
            backfill.finish(job)
            return
        }
        if current {
            // 以后主线再给这段认人时让出（窗口已送出，这里只防同一段又被主线记一遍）
            replaced.append((job.speaker, job.streamStart - 0.3, job.streamEnd + 0.3))
        }
        let body: [String: Any] = ["slotKey": job.slotKey, "t0": Int(job.t0), "t1": Int(job.t1), "text": result.text,
                                   "lang": result.locale, "langConfirmed": confirmed]
        backfillBusy = true
        Task.detached(priority: .background) { [weak self] in
            do {
                let reply = try await NativeAmbientServer.post("api/ambient/revise", body: body)
                let replacedRows = reply["replaced"] as? Int ?? 0
                self?.work.async {
                    self?.backfillBusy = false
                    self?.stats.revised += 1
                    self?.backfill.finish(job)
                    if replacedRows == 0 {
                        NativeAmbientLog.note("逐段重转：服务器时间轴上没找到 \(job.slotKey) 这段（已按新句补记）")
                    }
                }
            } catch {
                NativeAmbientLog.note("逐段重转：修正服务器记录失败 \(error.localizedDescription)，稍后重试", level: "error")
                self?.work.async {
                    self?.backfillBusy = false
                    self?.backfill.retry(job)
                }
            }
        }
    }

    /// 这个人的这一段要不要补线重转、用什么语言。nil = 不用（主线的「我的语言」已覆盖）。
    ///   - 「我」→ nil；
    ///   - 认出的人登记了语言：和我的语言一样 → nil；不一样 → 用它（已确认）；
    ///   - 推测票数：多数（≥75%）是别的语言 → 用它（待确认）；多数是我的语言且 ≥2 票 → nil；
    ///   - 否则拿这段去推测（每人最多 3 段、每段 ≥1.2 秒、积压时不推测）。
    private func segmentPlan(speaker: Int, duration: Double) -> (locale: String?, confirmed: Bool)? {
        if speaker == diarizer?.userIndex || slotNames[speaker] == "我" || slotPersonIds[speaker] == "me" { return nil }
        if let person = slotPersonIds[speaker] {
            if let registered = personLanguage[person] {
                if !registered.isEmpty { return registered == locale ? nil : (registered, true) }
            } else {
                fetchPersonLanguage(person)
            }
        }
        if let votes = slotLangVotes[speaker], let top = votes.max(by: { $0.value < $1.value }) {
            let share = Double(top.value) / Double(max(1, votes.values.reduce(0, +)))
            if share >= 0.75 {
                if top.key != locale { return (top.key, false) }
                if top.value >= 2 { return nil }
            }
        }
        guard (guessCount[speaker] ?? 0) < Self.maxGuessesPerSpeaker, duration >= Self.guessMinSeconds,
              backfill.count < NativeAmbientBackfill.maxJobs / 2,
              now - (lastGuessAt[speaker] ?? -.infinity) >= 2 else { return nil }
        return (nil, false)
    }

    private func fetchPersonLanguage(_ person: String) {
        personLanguage[person] = personLanguage[person]   // 占位防重复
        Task.detached(priority: .utility) { [weak self] in
            let language = await NativeSpeakerEmbedder.shared.language(of: person) ?? ""
            self?.work.async { self?.personLanguage[person] = language }
        }
    }

    private func topVote(_ speaker: Int) -> String? {
        slotLangVotes[speaker]?.max(by: { $0.value < $1.value })?.key
    }

    private func logStats() {
        guard Date().timeIntervalSince(lastStatsAt) >= 30 else { return }
        lastStatsAt = Date()
        let line = "旁听统计：主线 \(stats.streamed) 句；重转排队 \(stats.cut) 段、已转 \(stats.done)（空 \(stats.empty)、修正 \(stats.revised)），"
            + "队列里还有 \(backfill.count)；本窗 \(utterances.count) 句，已送 \(stats.sent) 窗"
            + (diarizer == nil ? "（分离器未就绪）" : "")
        // 下面这行是给排查用的实时数字，每 30 秒都写（上面那行没变化时不重复写）
        var detail = "旁听诊断：识别任务 \(recStats.tasks)、中间结果 \(recStats.partials)、定稿 \(recStats.finals)、出错 \(recStats.errors)"
            + (recStats.lastError != 0 ? "（最近 code=\(recStats.lastError)）" : "")
        if let level = gain.drainStats() {
            detail += String(format: "；电平 平均 %.0f dBFS、峰值 %.0f dBFS，增益 ×%.1f", level.rmsDB, level.peakDB, level.gain)
        }
        if let diarizer {
            let audio = diarizer.elapsed
            let factor = audio > 0 ? diarizer.busySeconds / audio : 0
            detail += String(format: "；分离器 实时率 %.2f、积压 %.1f 秒", factor, Double(diarizer.pendingSamples) / NativeStreamDiarizer.sampleRate)
        }
        detail += String(format: "；周期最大间隔 %.1f 秒", workMaxGap) + (diarizerResets > 0 ? "；分离器重建 \(diarizerResets) 次" : "")
        workMaxGap = 0
        NativeAmbientLog.note(detail)
        guard line != lastStatsLine else { return }
        lastStatsLine = line
        NativeAmbientLog.note(line)
    }

    private func flushWindow(force: Bool) {
        utterances.sort { $0.start < $1.start }   // 分段转写可能不按时间先后回来
        guard let first = utterances.first, let last = utterances.last else {
            windowStartedAt = Date()
            return
        }
        let t = now
        let chars = utterances.reduce(0) { $0 + $1.text.count }
        let due = force || t - last.end >= Self.windowGap || last.end - first.start >= Self.windowMaxSeconds
            || chars >= Self.windowMaxChars
        guard due else { windowDueSince = nil; return }
        // 只等「比这窗最后一句还早」的段转完；之后的段归下一窗。等太久（转写积压）也照送，别让整窗卡死
        let blocking = !pendingFinals.isEmpty   // 主线还有定稿没认人：等一下（逐段重转不等，它在后台事后修正）
        if blocking && !force {
            if windowDueSince == nil { windowDueSince = Date() }
            guard let since = windowDueSince, Date().timeIntervalSince(since) >= Self.windowWaitLimit else { return }
            NativeAmbientLog.note("旁听：主线定稿迟迟没认完人，已等 \(Int(Self.windowWaitLimit)) 秒，先送这一窗")
        }
        windowDueSince = nil
        let taken = utterances.sorted { $0.start < $1.start }
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
                    "t0": max(0, utterance.start - first.start), "t1": max(0, utterance.end - first.start),
                    "slotKey": utterance.speaker.map(slotKey) ?? "",
                    "lang": utterance.locale ?? locale, "langConfirmed": utterance.langConfirmed]
        }
        let speakerRows: [[String: Any]] = Set(taken.compactMap(\.speaker)).sorted().map { index in
            var row: [String: Any] = ["slotKey": slotKey(index)]
            if let person = slotPersonIds[index] { row["personId"] = person }
            else if index == diarizer?.userIndex { row["personId"] = "me" }
            if let vector = slotVectors[index] { row["vector"] = vector }
            return row
        }
        let speakers = Set(taken.compactMap(\.speaker)).count
        collectHeard(taken)
        let window = Window(id: "amb-" + Self.stamp.string(from: started) + "-" + String(UUID().uuidString.prefix(4)),
                            startedAt: Date(timeIntervalSince1970: epochMs(first.start) / 1000),
                            endedAt: Date(timeIntervalSince1970: epochMs(last.end) / 1000),
                            streamStart: first.start, streamEnd: last.end,
                            utterances: rows, speakerCount: max(1, speakers), locale: locale,
                            source: source.wireName, speakers: speakerRows)
        stats.sent += 1
        emit(.window(window))
    }

    // MARK: 熟人：声纹特征比对（不占分离器槽位，人数不限）

    /// 找一个「说够 3 秒还没比过」或「又多说了 10 秒」的槽位，取它最近 ≤10 秒的定稿语音算特征去比。
    /// 一次只比一个（嵌入模型在神经引擎上排队也要时间）。
    private func identifyNextSlot() {
        guard !identifying, let diarizer else { return }
        let ringEnd = ringStart + Double(ring.count) / NativeStreamDiarizer.sampleRate
        for (index, speech) in diarizer.finalizedSpeechSeconds().sorted(by: { $0.value > $1.value }) {
            let checks = slotChecks[index] ?? 0
            let checked = slotCheckedSpeech[index] ?? 0
            guard speech >= 3, checks < Self.identifyMaxChecks,
                  checks == 0 || speech - checked >= Self.identifyRecheckSpeech else { continue }
            slotCheckedSpeech[index] = speech
            var pieces: [[Float]] = []
            var total = 0
            for range in diarizer.finalizedSpeech(of: index).reversed() {
                let from = range.lowerBound + diarizerOrigin, to = range.upperBound + diarizerOrigin
                guard to <= ringEnd, from >= ringStart else { continue }
                let a = Int((from - ringStart) * NativeStreamDiarizer.sampleRate)
                let b = Int((to - ringStart) * NativeStreamDiarizer.sampleRate)
                guard b > a else { continue }
                pieces.append(ring[a..<b].map { Float($0) / 32_768 })
                total += b - a
                if total >= NativeSpeakerEmbedder.chunkSamples { break }
            }
            let voiced = NativeVoiceprint.voicedOnly(pieces.reversed().flatMap { $0 })
            guard voiced.count >= NativeSpeakerEmbedder.minimumSamples else { continue }
            slotChecks[index] = checks + 1
            identifying = true
            let session = diarizerSession
            Task.detached(priority: .utility) { [weak self] in
                do {
                    let vector = try await NativeSpeakerEmbedder.shared.embedding(of: voiced)
                    let match = await NativeSpeakerEmbedder.shared.identify(vector)
                    self?.work.async { self?.identified(slot: index, match: match, vector: vector, session: session) }
                } catch {
                    NativeAmbientLog.note("声纹比对：说话人\(index + 1) 的特征算不出来 \(error.localizedDescription)", level: "error")
                    self?.work.async { self?.identifying = false }
                }
            }
            return
        }
    }

    private func slotKey(_ index: Int) -> String { sessionID + ":" + String(index) }

    private func identified(slot: Int, match: NativeSpeakerEmbedder.Match, vector: [Float], session: Int) {
        identifying = false
        guard session == diarizerSession else { return }
        slotVectors[slot] = vector
        if let person = match.personId, match.name != nil { slotPersonIds[slot] = person }
        let detail = String(format: "最近 %@ %.2f，次近 %.2f", match.nearest ?? "无", match.distance, match.runnerUp)
        if let name = match.name {
            if slotNames[slot] != name {
                let before = slotNames[slot]
                slotNames[slot] = name
                heard.removeAll { $0.session == session && $0.slot == slot }
                emit(.heard(heard))
                NativeAmbientLog.note("声纹比对：说话人\(slot + 1) → \(name)" + (before.map { "（原来认成 \($0)）" } ?? "") + "（\(detail)）")
            }
        } else {
            // 没认出时保留之前的结论：一次样本不好不该把已认出的人抹掉
            NativeAmbientLog.note("声纹比对：说话人\(slot + 1) 不是已登记的人（\(detail)）")
        }
    }

    private func collectHeard(_ taken: [Utterance]) {
        var byIndex: [Int: [Utterance]] = [:]
        for utterance in taken {
            guard let index = utterance.speaker, index != diarizer?.userIndex, slotNames[index] == nil else { continue }
            byIndex[index, default: []].append(utterance)
        }
        guard !byIndex.isEmpty else { return }
        let fresh = byIndex.map { index, rows in
            HeardSpeaker(id: UUID().uuidString, label: "说话人\(index + 1)",
                         sample: String(rows.map(\.text).joined(separator: " ").prefix(60)),
                         ranges: rows.map { $0.start...$0.end }, heardAt: Date(), slot: index, session: diarizerSession,
                         slotKey: slotKey(index))
        }
        // 同一会话同一槽位只留最新的一条
        let freshSlots = Set(fresh.map(\.slot))
        heard = Array((fresh + heard.filter { $0.session != diarizerSession || !freshSlots.contains($0.slot) }).prefix(8))
        emit(.heard(heard))
    }

    /// 从环形缓冲里取出这个人的说话片段存成熟人声纹。只取还在缓冲里（最近 4 分钟）的部分。
    func namePerson(_ speaker: HeardSpeaker, name: String, done: @escaping (String) -> Void) {
        work.async {
            var samples: [Float] = []
            for range in speaker.ranges {
                let a = Int((range.lowerBound - self.ringStart) * NativeStreamDiarizer.sampleRate)
                let b = Int((range.upperBound - self.ringStart) * NativeStreamDiarizer.sampleRate)
                guard a >= 0, b > a, b <= self.ring.count else { continue }
                samples.append(contentsOf: self.ring[a..<b].map { Float($0) / 32_768 })
            }
            let voiced = NativeVoiceprint.voicedOnly(samples)
            let seconds = Double(voiced.count) / NativeStreamDiarizer.sampleRate
            guard seconds >= 3 else {
                let message = samples.isEmpty ? "这段声音已经滚出缓冲（超过 4 分钟），等他再说话后重试"
                    : String(format: "他的有效语音只有 %.1f 秒，至少要 3 秒；等他多说几句再起名", seconds)
                NativeAmbientLog.note("熟人声纹：\(name) 没保存 —— \(message)")
                done(message)
                return
            }
            do {
                let total = try NativeVoiceprint.savePerson(name: name, samples: voiced)
                self.heard.removeAll { $0.id == speaker.id }
                self.emit(.heard(self.heard))
                // 起名即生效：这个槽位就是他，不必等比对
                if speaker.session == self.diarizerSession { self.slotNames[speaker.slot] = name }
                // 同步到服务器：块归这个人（同名即同一人，KJ 人物节点随之建立或复用），声纹特征进他的声纹库
                let key = speaker.slotKey
                Task.detached(priority: .utility) {
                    var body: [String: Any] = ["slotKey": key, "name": name]
                    if let vector = try? await NativeSpeakerEmbedder.shared.embedding(of: voiced) { body["vector"] = vector }
                    do {
                        let reply = try await NativeAmbientServer.post("api/ambient/slots/assign", body: body)
                        let person = (reply["person"] as? [String: Any])?["id"] as? String
                        await NativeSpeakerEmbedder.shared.invalidateServer()
                        self.work.async { if let person, speaker.session == self.diarizerSession { self.slotPersonIds[speaker.slot] = person } }
                        NativeAmbientLog.note("熟人声纹：「\(name)」已同步到服务器（KJ 人物页）")
                    } catch {
                        NativeAmbientLog.note("熟人声纹：「\(name)」同步服务器失败 \(error.localizedDescription)", level: "error")
                    }
                }
                let message = String(format: "已记住「%@」（样本共 %.0f 秒），之后靠声纹比对认出他", name, total)
                NativeAmbientLog.note("熟人声纹：" + message)
                done(message)
            } catch {
                NativeAmbientLog.note("熟人声纹：\(name) 保存失败 \(error.localizedDescription)", level: "error")
                done("保存失败：\(error.localizedDescription)")
            }
        }
    }

    /// 判断回执里的 names：服务器已知的块 → 人（用户在时间轴上事后起的名 / 合并后的名字），当前会话的块立刻改标。
    func applyServerNames(_ names: [String: Any]) {
        work.async {
            for (key, value) in names {
                guard let info = value as? [String: Any], let name = info["name"] as? String else { continue }
                let parts = key.split(separator: ":")
                guard parts.count == 2, String(parts[0]) == self.sessionID, let index = Int(parts[1]) else { continue }
                if self.slotNames[index] != name {
                    self.slotNames[index] = name
                    NativeAmbientLog.note("旁听：服务器把说话人\(index + 1) 标为「\(name)」")
                }
                if let person = info["personId"] as? String { self.slotPersonIds[index] = person }
                self.heard.removeAll { $0.session == self.diarizerSession && $0.slot == index }
            }
            self.emit(.heard(self.heard))
        }
    }

    /// 熟人列表变了（删除）：去掉已不存在的名字，所有槽位重新比对。
    func reloadPeople() {
        work.async {
            let names = Set(NativeVoiceprint.people().map(\.name))
            self.slotNames = self.slotNames.filter { $0.value == "我" || names.contains($0.value) }
            self.slotChecks = [:]
            self.slotCheckedSpeech = [:]
        }
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
