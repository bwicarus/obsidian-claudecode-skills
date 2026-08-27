import AVFoundation
import Foundation
import WatchConnectivity

/// 手表「按住说话」。
///
/// ## 为什么这一版是按住说话
///
/// ⚠ **这是过渡形态。** 用户要的是「按一下开始桥接电脑上的通话」，
/// 终态设计见 `references/watch-companion.md`。
///
/// 这一版之所以是回合制：走的是 WCSession 经手机中转，而 Windows 那条语音桥
/// 是 48kHz 连续双工、每方向 97.8 KB/s，WCSession 最快的通道单条上限 64KB
/// 且是串行的电源管理 IPC 队列 —— **传输类型不对**，不是调优问题。
///
/// 手表自己直连的路：WebSocket 被 watchOS 禁（TN3135），而解禁它要 CallKit，
/// 但 CallKit 通话中手表只显示系统通话 UI（用户 2026-08-27 实测）——
/// 我们自己的界面显示不出来，所以那条路作废。
/// 现在的方向是**普通 HTTPS 流式 + WKExtendedRuntimeSession**，
/// 不需要任何豁免，界面归我们。
///
/// ## 为什么录 m4a 不录 WAV
///
/// 16kHz mono s16 的 WAV 是 32000 B/s，64KB 上限只够 **2 秒** —— 没法用。
/// AAC 同采样率约 3000 B/s，同样的上限能录 20 秒。代价是 Pi 侧要多一次
/// ffmpeg 转码（`/api/voice/transcribe` 本来就有这条分支），短片段很划算。
@MainActor
final class WatchVoice: NSObject, ObservableObject {
    /// 录音上限。到点自动停 —— 手表上没有"我忘了松手"的补救，而超了
    /// 64KB 整条消息发不出去。
    static let maximumSeconds: TimeInterval = 18
    /// 留 4KB 余量给消息信封。
    static let maximumBytes = 61_440

    enum Phase: Equatable {
        case idle
        case recording
        case sending
        case thinking
        case done(heard: String, reply: String)
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle

    private var recorder: AVAudioRecorder?
    private var fileURL: URL?
    private var autoStop: Timer?
    /// 这一轮是什么时候发出去的。用来分辨"刚回来的结果"和"上次的旧结果"——
    /// 快照里带着上一轮，不加这道闸会把旧答案当成新答案显示。
    private var sentAtMs: Double = 0
    private let speaker = AVSpeechSynthesizer()

    // ── 按下 ──

    func begin() {
        guard phase == .idle || isFinished else { return }
        phase = .recording
        Task { await start() }
    }

    private var isFinished: Bool {
        if case .done = phase { return true }
        if case .failed = phase { return true }
        return false
    }

    private func start() async {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.playAndRecord, mode: .default)
            try session.setActive(true)
        } catch {
            phase = .failed("打不开麦克风：" + error.localizedDescription)
            return
        }
        // watchOS 的录音权限是异步授予的。没授权时**必须说清楚**，
        // 否则表现为"按了没反应"——用户会以为是坏了。
        let granted = await withCheckedContinuation { continuation in
            session.requestRecordPermission { continuation.resume(returning: $0) }
        }
        guard granted else {
            phase = .failed("没有麦克风权限，去手机的 Watch App 里开")
            return
        }

        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("watch-turn.m4a")
        try? FileManager.default.removeItem(at: url)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 16_000.0,          // STT 就要 16k，别录高了再降
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 24_000,        // 见文件头那笔预算
        ]
        do {
            let made = try AVAudioRecorder(url: url, settings: settings)
            made.record()
            recorder = made
            fileURL = url
        } catch {
            phase = .failed("录音起不来：" + error.localizedDescription)
            return
        }
        autoStop = Timer.scheduledTimer(
            withTimeInterval: Self.maximumSeconds, repeats: false
        ) { [weak self] _ in
            Task { @MainActor in self?.finish() }
        }
    }

    // ── 松开 ──

    func finish() {
        autoStop?.invalidate()
        autoStop = nil
        guard phase == .recording, let recorder, let fileURL else { return }
        recorder.stop()
        self.recorder = nil
        phase = .sending

        guard let data = try? Data(contentsOf: fileURL), !data.isEmpty else {
            phase = .failed("没录到声音")
            return
        }
        guard data.count <= Self.maximumBytes else {
            // 说清是长度问题而不是笼统的失败 —— 用户下次知道说短点。
            phase = .failed("说得太长了（\(data.count / 1024)KB），再短一点")
            return
        }
        send(data)
    }

    func cancel() {
        autoStop?.invalidate()
        autoStop = nil
        recorder?.stop()
        recorder = nil
        phase = .idle
    }

    // ── 送去手机跑一轮 ──

    private func send(_ data: Data) {
        let session = WCSession.default
        guard session.activationState == .activated else {
            phase = .failed("还没连上手机")
            return
        }
        guard session.isReachable else {
            // 手机不在旁边时**当场说**，不要静默排队：语音是即时交互，
            // 半小时后才送达的一句话毫无意义。
            phase = .failed("手机不在旁边，手表只能借手机说话")
            return
        }
        phase = .thinking
        sentAtMs = Date().timeIntervalSince1970 * 1000
        // ⚠ 这里的 reply **只是回执**，不是结果。
        // 那一轮最坏要 105 秒（转写 45s + 问答 60s），而 WCSession 的 reply
        // 有超时（WCError.messageReplyTimedOut）—— 在 reply 里等完必然超时。
        // 手机先回执、再干活，结果经 WatchLink 另发一条回来（见 observe）。
        session.sendMessageData(
            data,
            replyHandler: { _ in },
            errorHandler: { [weak self] error in
                Task { @MainActor in
                    self?.phase = .failed("发送失败：" + error.localizedDescription)
                }
            })
    }

    /// 观察 WatchLink 收到的结果。
    ///
    /// WCSession 只能有一个 delegate（WatchLink 占着），所以结果由它统一收，
    /// 这里只是取用 —— 而不是两个类抢同一个 delegate。
    func observe(_ link: WatchLink) {
        guard let turn = link.lastTurn, turn.turnAtMs > sentAtMs else { return }
        // ⚠ 状态闸要在**错误分支之前** —— 否则上一轮迟到的失败会把用户
        // 正在录的这一轮打断成"失败"。只有在等结果时才接受结果。
        guard phase == .thinking || phase == .sending else { return }
        if let error = turn.error {
            phase = .failed(error)
            return
        }
        phase = .done(heard: turn.heard, reply: turn.reply)
        speak(turn.reply)
    }

    /// 用手表本地的合成器念回答。
    ///
    /// ⚠ 刻意**不**从手机传音频过来：那要再多一次 WCSession 往返，而回答
    /// 音频比录音大得多。本地合成即时、零传输，音色差一点换来的是能用。
    private func speak(_ text: String) {
        guard !text.isEmpty else { return }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "zh-CN")
        speaker.speak(utterance)
    }
}
