#if os(watchOS)

import AVFoundation
import Combine
import Foundation
import Network
import WatchKit

/// 手表低层网络豁免的真机探针。
///
/// ## 在验什么
///
/// TN3135 有三条低层网络豁免，第一条是「app 正在流式播放音频时」（watchOS 6+）。
/// 公开参考实现 `leptos-null/WatchOS-WebSocket` 已在 Series 7 + Series 10 /
/// watchOS 26.6 上证实：**有活动音频会话就能开 WebSocket，不需要 CallKit**。
///
/// 但它验的是 `.playback`（只放不录）。我们要的是双工，得用 `.playAndRecord`，
/// **那个组合没有任何硬件验证报告**。这个探针就是补这一枪。
///
/// ## ⚠ 那个让所有人以为此路不通的静默陷阱
///
/// `AVAudioSession.setActive()` **不抛错，但也不解禁 WebSocket**；
/// 必须用异步的 `activate(options:completionHandler:)`。
/// 论坛上两个「证明双工不可能」的帖子（777373、781095）用的都是 `setActive`，
/// 都在模拟器上通、真机报 POSIX 50 —— 那不是硬件限制，是**拒绝的签名**。
///
/// 所以 `.syncTrap` 这一档是**负对照**：它应当失败。它失败才证明这套探针
/// 真的在测豁免，而不是在测别的什么东西。
///
/// ## ⚠ 为什么必须有对照组
///
/// 只测「我们要的那一档」是不够的：它失败时分不清是 `.playAndRecord` 不行、
/// 还是构建/网络/服务端哪里坏了。所以五档里有一正两负：
///
/// | 档 | 作用 | 预期 |
/// |---|---|---|
/// | `.playbackControl` | **正对照** —— 参考实现的原配置 | 应当通 |
/// | `.duplexQuiet` | 我们要的：双工会话，不跑引擎 | ? |
/// | `.duplexLive` | 我们要的：双工会话 + 麦克风真的在跑 | ? |
/// | `.syncTrap` | **负对照** —— 同样配置但用同步 setActive | 应当失败 |
/// | `.noAudio` | **负对照** —— 完全不碰音频会话 | 应当失败 |
///
/// 两个负对照都失败、正对照通过，中间两档的结果才可信。
/// 这是 `references/evidence-quality-lessons.md` 那条「一个信号只有一种解释时
/// 才能单独采」的落地。
///
/// ## ⚠ 判据只从落盘的日志读，不从屏幕上读
///
/// 挂着 Xcode 调试器时 app 不会被挂起 —— 那测的是调试器不是系统；不挂调试器
/// 就没有 stdout。而这里要测的恰恰是「放下手腕 / 按数码表冠之后还活不活着」，
/// 那时候没人在看屏幕。
///
/// 所以每一条都**当场追加写文件**（不缓存、不等退出时统一写）：进程随时可能
/// 被系统收走，缓存在内存里的记录会跟着一起消失，而那正是最该被记下来的时刻。
///
/// ## 不需要任何服务端
///
/// 打的是公共 echo 服务（跟参考实现同一个）。**只发序号和 app 状态**，不发
/// 音频、不发任何个人数据。这样也就不需要在 Pi 上开端口、不需要动 Funnel。
@MainActor
final class WatchNetworkProbe: ObservableObject {

    /// 五档配置。顺序即建议的测试顺序：先跑正对照确认探针本身是好的。
    enum Mode: String, CaseIterable, Identifiable {
        case playbackControl = "A 正对照·只放"
        case duplexQuiet     = "B 双工·不跑麦"
        case duplexLive      = "C 双工·麦在跑"
        case syncTrap        = "D 负对照·同步"
        case noAudio         = "E 负对照·无音频"

        var id: String { rawValue }

        /// 这一档预期是什么结果。写在 UI 上，免得测完了还要回来查文档。
        var expectation: String {
            switch self {
            case .playbackControl: return "应当连上（参考实现验过）"
            case .duplexQuiet, .duplexLive: return "未知 —— 这就是要测的"
            case .syncTrap: return "应当连不上（同步 setActive 的陷阱）"
            case .noAudio: return "应当连不上（没有豁免）"
            }
        }
    }

    enum ProbeError: LocalizedError {
        case microphoneDenied
        var errorDescription: String? {
            // 说清该去哪儿修 —— 手表上没有设置入口，光说"失败"用户没法动。
            "没有麦克风权限，去手机的 Watch App 里开"
        }
    }

    enum State: Equatable {
        case idle
        case preparing
        /// Network framework 从这里会无限重试。**手表上的豁免拒绝就落在这个状态**
        /// （POSIX 50 / "Network is down"），不是 `.failed`。守着 failed 会一直等不到。
        case waiting(String)
        case connected(echoes: Int)
        case failed(String)
        case stopped(echoes: Int, heldSeconds: Int)
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var mode: Mode = .playbackControl
    /// 屏幕上滚动显示的最近几行。**只是给人看的**，判据在文件里。
    @Published private(set) var tail: [String] = []

    private var connection: NWConnection?
    private var engine: AVAudioEngine?
    private var beat: Task<Void, Never>?
    private var startedAt: Date?
    private var echoes = 0
    private var audioActivated = false

    private static let endpoint = URL(string: "wss://echo.websocket.org")!
    /// 心跳间隔。2 秒是权衡：够密才能看清「哪一刻断的」，又不至于把日志淹掉。
    private static let beatSeconds: UInt64 = 2

    // ── 落盘 ──

    /// 日志文件。放 Documents 是刻意的：caches 会被系统在压力下清掉，
    /// 而这份记录的价值恰恰在于「系统压力大的时候发生了什么」。
    static var logURL: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("watch-net-probe.jsonl")
    }

    /// 追加一行。**每条当场落盘**，理由见类型注释。
    private func log(_ event: String, _ extra: [String: Any] = [:]) {
        var row: [String: Any] = [
            "at": Date().timeIntervalSince1970,
            "mode": mode.rawValue,
            "event": event,
            // 这一条是整份记录里最重要的字段：断掉那一刻 app 在前台还是后台，
            // 决定了结论是「豁免不存续」还是「进程被收走」——两者的修法完全不同。
            "appState": Self.appStateName(),
        ]
        extra.forEach { row[$0.key] = $0.value }

        guard let data = try? JSONSerialization.data(withJSONObject: row),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"

        let url = Self.logURL
        if let handle = try? FileHandle(forWritingTo: url) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: Data(line.utf8))
        } else {
            try? Data(line.utf8).write(to: url)
        }

        tail.append(Self.brief(event, extra))
        if tail.count > 12 { tail.removeFirst(tail.count - 12) }
    }

    private static func appStateName() -> String {
        switch WKApplication.shared().applicationState {
        case .active: return "active"
        case .inactive: return "inactive"
        case .background: return "background"
        @unknown default: return "unknown"
        }
    }

    private static func brief(_ event: String, _ extra: [String: Any]) -> String {
        let stamp = DateFormatter.localizedString(
            from: Date(), dateStyle: .none, timeStyle: .medium)
        if let n = extra["n"] as? Int { return "\(stamp) \(event) #\(n)" }
        if let why = extra["why"] as? String { return "\(stamp) \(event) \(why)" }
        return "\(stamp) \(event)"
    }

    // ── 开始 ──

    func start(_ picked: Mode) {
        guard case .idle = state else { return }
        mode = picked
        state = .preparing
        echoes = 0
        startedAt = Date()
        log("start", ["expect": picked.expectation])
        Task { await run() }
    }

    private func run() async {
        do {
            try await activateAudio()
        } catch {
            // ⚠ 音频起不来要**当场说清是哪一步**：如果这里就挂了，
            // 后面 WebSocket 连不上跟豁免没关系，是我们自己没准备好。
            log("audio-failed", ["why": String(describing: error)])
            state = .failed("音频会话没起来：\(error.localizedDescription)")
            return
        }
        openSocket()
    }

    /// 按当前档位准备音频会话。
    private func activateAudio() async throws {
        let session = AVAudioSession.sharedInstance()

        switch mode {
        case .noAudio:
            log("audio-skipped")
            return

        case .playbackControl:
            // 参考实现的原配置，一字不改。
            try session.setCategory(.playback)
            try await session.activate(options: [])
            audioActivated = true
            log("audio-active", ["category": "playback", "how": "async activate",
                                 "route": routeNames(session)])

        case .duplexQuiet, .duplexLive:
            // ⚠ **必须先要麦克风权限**，否则 `.playAndRecord` 激活会失败，
            // 而那个失败长得跟"豁免不解禁"一模一样 —— 会把整个实验带偏。
            // 这正是 evidence-quality-lessons 那条「一个信号只有一种解释时
            // 才能单独采」：不先排除权限，这一档的失败就有两种解释。
            let granted = await withCheckedContinuation { continuation in
                session.requestRecordPermission { continuation.resume(returning: $0) }
            }
            log("mic-permission", ["granted": granted])
            guard granted else {
                throw ProbeError.microphoneDenied
            }
            // 我们真正要的：双工。`.voiceChat` 打开回声消除。
            try session.setCategory(.playAndRecord, mode: .voiceChat)
            try await session.activate(options: [])
            audioActivated = true
            log("audio-active", ["category": "playAndRecord", "mode": "voiceChat",
                                 "how": "async activate", "route": routeNames(session)])
            if mode == .duplexLive { try startEngine() }

        case .syncTrap:
            // 同上：先排除权限这个干扰项，否则失败有两种解释。
            let allowed = await withCheckedContinuation { continuation in
                session.requestRecordPermission { continuation.resume(returning: $0) }
            }
            log("mic-permission", ["granted": allowed])
            guard allowed else { throw ProbeError.microphoneDenied }
            // 负对照：同样的类别，但用同步的 setActive。
            // ⚠ 它**不会抛错** —— 这正是陷阱本身。所以这里的"成功"没有意义，
            // 有意义的是接下来 WebSocket 连不连得上。
            try session.setCategory(.playAndRecord, mode: .voiceChat)
            try session.setActive(true)
            audioActivated = true
            log("audio-active", ["category": "playAndRecord", "how": "sync setActive",
                                 "note": "预期这一档连不上", "route": routeNames(session)])
        }
    }

    private func routeNames(_ session: AVAudioSession) -> [String] {
        session.currentRoute.outputs.map(\.portName)
    }

    /// 真的把麦克风跑起来（C 档）。
    ///
    /// 有一种可能是「活动会话」还不够、要真的有音频在流动。参考实现那条
    /// `4e322c1 Remove unneeded audio playback` 说明对 `.playback` 不需要，
    /// 但对 `.playAndRecord` 没人验过 —— 所以 B 和 C 分开测。
    private func startEngine() throws {
        let made = AVAudioEngine()
        let input = made.inputNode
        input.installTap(onBus: 0, bufferSize: 1024,
                         format: input.outputFormat(forBus: 0)) { _, _ in
            // 刻意什么都不做：这里只是让音频真的流动起来。
            // **绝不把音频写进日志或送出去** —— 这是个网络探针，不是录音机。
        }
        try made.start()
        engine = made
        log("engine-started")
    }

    // ── WebSocket ──

    private func openSocket() {
        let options = NWProtocolWebSocket.Options()
        options.autoReplyPing = true

        let parameters: NWParameters = .tls
        parameters.defaultProtocolStack.applicationProtocols.insert(options, at: 0)
        // TN3135 讲的是给音频流量打标记；这是 Network framework 侧最接近的对应物。
        parameters.serviceClass = .interactiveVoice

        // ⚠ 用 NWConnection 而不是 URLSessionWebSocketTask —— Apple DTS 的 Quinn
        // 点名：watchOS 上「每个 session 都有点像后台 session，实际工作在进程外做」，
        // 所以 URLSession 那条在手表上行为不同，不能拿来判豁免。
        let made = NWConnection(to: .url(Self.endpoint), using: parameters)
        connection = made

        made.stateUpdateHandler = { [weak self] newState in
            Task { @MainActor in self?.handle(newState) }
        }
        made.start(queue: .main)
        receiveNext(on: made)
    }

    private func handle(_ newState: NWConnection.State) {
        switch newState {
        case .ready:
            log("connected")
            state = .connected(echoes: echoes)
            startBeating()

        case .waiting(let error):
            // ⚠ **豁免被拒就落在这里**，不是 .failed。
            // POSIX 50 / "Network is down" 是拒绝的签名（TN3135 的形态）。
            let why = String(describing: error)
            log("waiting", ["why": why,
                            "isDenial": why.contains("50") || why.contains("Network is down")])
            state = .waiting(why)

        case .failed(let error):
            log("failed", ["why": String(describing: error)])
            state = .failed(String(describing: error))
            teardown()

        case .cancelled:
            log("cancelled")

        case .preparing, .setup:
            break

        @unknown default:
            break
        }
    }

    private func receiveNext(on connection: NWConnection) {
        connection.receiveMessage { [weak self] _, _, _, error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.log("receive-error", ["why": String(describing: error)])
                    return
                }
                self.echoes += 1
                // 每 10 次记一条就够 —— 密了会把「断在哪一刻」淹掉。
                if self.echoes % 10 == 1 { self.log("echo", ["n": self.echoes]) }
                if case .connected = self.state { self.state = .connected(echoes: self.echoes) }
                self.receiveNext(on: connection)
            }
        }
    }

    private func startBeating() {
        beat?.cancel()
        beat = Task { [weak self] in
            var n = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(Self.beatSeconds))
                guard let self, let connection = self.connection else { return }
                n += 1
                let payload = ["n": n, "appState": Self.appStateName()] as [String: Any]
                guard let data = try? JSONSerialization.data(withJSONObject: payload) else { continue }
                let meta = NWProtocolWebSocket.Metadata(opcode: .text)
                let context = NWConnection.ContentContext(identifier: "beat", metadata: [meta])
                connection.send(content: data, contentContext: context,
                                isComplete: true, completion: .contentProcessed { error in
                    if let error {
                        Task { @MainActor in
                            self.log("send-error", ["n": n, "why": String(describing: error)])
                        }
                    }
                })
                // 每 15 次（30 秒）留一个活着的证据，这样即使一直没断，
                // 也能从日志读出「它撑了多久」而不是只有开头一条。
                if n % 15 == 0 { self.log("alive", ["n": n]) }
            }
        }
    }

    // ── 停止 ──

    func stop() {
        let held = Int(Date().timeIntervalSince(startedAt ?? Date()))
        log("stop", ["echoes": echoes, "heldSeconds": held])
        state = .stopped(echoes: echoes, heldSeconds: held)
        teardown()
    }

    private func teardown() {
        beat?.cancel()
        beat = nil
        connection?.cancel()
        connection = nil
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        engine = nil
        if audioActivated {
            try? AVAudioSession.sharedInstance().setActive(false)
            audioActivated = false
        }
    }

    func reset() {
        teardown()
        state = .idle
        tail = []
    }

    // ── 导出 ──

    // ── 把日志读成结论 ──

    /// 一档的战报。
    ///
    /// ⚠ 这是**分析**不是采集：原始 jsonl 一行不动地留着。
    /// 之所以要在手表上算，是因为整个实验里最关键的那个问题 ——
    /// 「放下手腕之后还活着吗」—— 恰恰发生在没人看屏幕的时候，
    /// 事后光看最后那几行 tail 读不出来。
    struct Verdict: Identifiable {
        let mode: String
        var connected = false
        /// 豁免被拒的签名（POSIX 50 / Network is down）。
        var denied = false
        var micDenied = false
        var heldSeconds = 0
        /// **这是整个实验的核心问题**：app 已经不在前台时，还有没有心跳。
        var beatsWhileBackground = 0
        var beatsWhileActive = 0

        var id: String { mode }

        /// 一句话结论。刻意分「没测到」和「测到了但是否定的」——
        /// 前者要重测，后者是答案。
        var line: String {
            if micDenied { return "⚠️ 麦克风没权限，这一档没测成" }
            if denied { return "🚫 豁免被拒（这正是 setActive 陷阱的签名）" }
            if !connected { return "❌ 没连上（原因见原始日志）" }
            if beatsWhileBackground > 0 {
                return "✅ 连上了，且**后台仍在跳** \(beatsWhileBackground) 次"
            }
            if heldSeconds < 20 { return "🟡 连上了但很快就停（\(heldSeconds)s），没测到后台" }
            return "🟡 连上了（\(heldSeconds)s），但全程都在前台 —— 后台没测到"
        }
    }

    /// 把 jsonl 读成每档一条战报。
    func verdicts() -> [Verdict] {
        guard let data = try? Data(contentsOf: Self.logURL),
              let text = String(data: data, encoding: .utf8) else { return [] }

        var byMode: [String: Verdict] = [:]
        var firstAt: [String: Double] = [:]
        var lastAt: [String: Double] = [:]

        for line in text.split(whereSeparator: \.isNewline) {
            guard let row = try? JSONSerialization.jsonObject(
                    with: Data(line.utf8)) as? [String: Any],
                  let mode = row["mode"] as? String,
                  let event = row["event"] as? String else { continue }

            var verdict = byMode[mode] ?? Verdict(mode: mode)
            let at = (row["at"] as? Double) ?? 0
            let appState = (row["appState"] as? String) ?? ""
            if firstAt[mode] == nil { firstAt[mode] = at }
            lastAt[mode] = at

            switch event {
            case "connected":
                verdict.connected = true
            case "mic-permission":
                if (row["granted"] as? Bool) == false { verdict.micDenied = true }
            case "waiting":
                if (row["isDenial"] as? Bool) == true { verdict.denied = true }
            case "alive", "echo":
                // 「后台还在跳」是这份记录里唯一无法从屏幕上取得的信号。
                if appState == "background" || appState == "inactive" {
                    verdict.beatsWhileBackground += 1
                } else {
                    verdict.beatsWhileActive += 1
                }
            default:
                break
            }
            byMode[mode] = verdict
        }

        return byMode.map { key, value in
            var one = value
            one.heldSeconds = Int((lastAt[key] ?? 0) - (firstAt[key] ?? 0))
            return one
        }.sorted { $0.mode < $1.mode }
    }

    /// 把落盘的日志读回来。
    ///
    /// 手表上没法看长文本，所以这份是给**手机**用的 —— 由 `WatchLink` 经
    /// WCSession 送过去。手表这边只显示最近几行确认它在跑。
    func exportLog() -> Data? {
        try? Data(contentsOf: Self.logURL)
    }

    var logSize: Int {
        let attributes = try? FileManager.default
            .attributesOfItem(atPath: Self.logURL.path)
        return (attributes?[.size] as? Int) ?? 0
    }

    func clearLog() {
        try? FileManager.default.removeItem(at: Self.logURL)
        tail = []
    }
}

#endif
