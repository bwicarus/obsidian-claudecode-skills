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
/// ## 📋 2026-08-27 第一轮真机实测结果（决定了第二版长什么样）
///
/// **A/B/C/D/E 全部连上，包括两个负对照。** 然后：
///
/// > **一息屏或者切后台就断**（`POSIXErrorCode 57: Socket is not connected`）。
///
/// 两条结论，第二条才是要害：
///
/// 1. **负对照没失败 → 这一轮不能把「连上」归因于音频豁免。**
///    最可能是测试顺序污染（先跑了 A，音频会话在进程里留下状态，后面搭便车）。
///    ⚠ 所以 D/E 必须在**刚打开 app、还没跑过任何其它档**时第一个测。
/// 2. **前台能连、后台就断** —— 这才是决定设计成不成立的那条。
///
/// 而第一版有个盲点让第 2 条没法往下查：**它分不清「豁免被收回」和「进程被
/// 挂起」**。第二版加了 `NWPathMonitor`（TN3135 点名：被拦时恒 `.unsatisfied`）
/// 来区分。
///
/// 还有个更可能的直接原因：**第一版从头到尾没有真的"播放"过声音**。
/// `UIBackgroundModes: [audio]` 的保活语义是「app **正在播放**音频时给额外
/// 运行时」，而 C 档只装了输入 tap（只录不放）。所以第二版加了 `.duplexCall`：
/// 边放（近静音循环）边录 —— 这也正是一通真通话的形状。
///
/// ## ⚠ 为什么必须有对照组
///
/// 只测「我们要的那一档」是不够的：它失败时分不清是 `.playAndRecord` 不行、
/// 还是构建/网络/服务端哪里坏了。所以：
///
/// | 档 | 作用 | 实测 |
/// |---|---|---|
/// | `.loadedCall` | **主力** —— F 的配置 + 真通话节奏（50 包/秒） | 第三版新增 |
/// | `.duplexCall` | 边放边录，闲连接 | ✅ **后台活下来了** |
/// | `.playbackControl` | **正对照** —— 参考实现的原配置 | 前台通 |
/// | `.duplexQuiet` | 双工会话，不跑引擎 | 前台通，后台断 |
/// | `.duplexLive` | 双工会话 + 只录不放 | 前台通，后台断 |
/// | `.syncTrap` | **负对照** —— 同步 setActive | ⚠ 本该失败，却通了 |
/// | `.noAudio` | **负对照** —— 完全不碰音频会话 | ⚠ 本该失败，却通了 |
///
/// ## 📋 2026-08-27 第二轮：F 档后台活下来了
///
/// **而且这次的对照是干净的**：B/C 与 F 的唯一差别就是「放不放声音」，
/// B/C 在后台断了、F 没断。所以「持续播放音频是后台存活的前提」这条
/// 有内部对照撑着，不是孤证。
///
/// 参考实现那条 `4e322c1 "Remove unneeded audio playback"` 的注解现在可以
/// 补完了：**对那个 demo 确实 unneeded，因为它只在前台跑。**
///
/// ⚠ **但 F 证明的是「一条闲着的连接」能活下来** —— 它每 2 秒发几个字节。
/// 真通话是每秒 50 个包，在 CPU、无线电唤醒频率、发热上完全不是一回事，
/// 而 `exceededResourceLimits` 恰恰被这些触发。所以有了 `.loadedCall`。
///
/// 这是 `references/evidence-quality-lessons.md` 那条「一个信号只有一种解释时
/// 才能单独采」的落地 —— 而第一轮恰好演示了反面：负对照一旦没失败，
/// 正面结果就**什么都证明不了**。
///
/// ## 📋 2026-08-27 第三轮：G 档的数字读不出结论（三处采集缺陷）
///
/// 实测：`撑了 198 秒 · 回声 1005 · send-error #3245`。三个问题一起暴露：
///
/// 1. **实际只跑了 16.4 Hz，不是设定的 50 Hz**（3245 包 / 198 秒），
///    而且**一声不吭**。原因是 `sleep(for:)` 每轮从"现在"重新起算，
///    循环体耗时和调度延迟一圈圈累积。→ 改成按**绝对时刻**排程
///    （`sleep(until:clock:)`），追不上时 `pace-lagging` 出声并重新对表
///    （不重新对表会变成烧 CPU 的空转，那测的就不是通话负载了）。
/// 2. **唯一那条 send-error 出现在最后一个包上** —— 很可能就是按「停止」时
///    拆连接的收尾竞态，而不是"跑着跑着断了"。→ 收尾期间的失败改记
///    `send-error-teardown`，跟运行中的失败分开。
/// 3. **屏幕上只有包序号，没有错误原文**。`brief()` 写成「有 n 就显示 n」，
///    于是把唯一能判断的东西折叠掉了 —— 对端限流 / 系统掐了 / 收尾竞态
///    三种解释都活着，整轮数据白采。→ **原因永远优先于序号。**
///
/// ⚠ 三处都是同一个毛病：**采集时把能分辨的信息丢了**。分析层再怎么写
/// 也救不回来（`evidence-quality-lessons.md`）。
///
/// ⚠ 还有一条没解决的：回声率 31%。打的是免费公共 echo 服务，
/// 50 包/秒很可能触发它的限流 —— 那样的话失败**归因于对端而不是 watchOS**。
/// 下一轮先看错误原文：说"连接被重置"就是对端，说别的才轮到平台。
/// 真要定论得换成自己的端点（Pi 的 Funnel），但在知道需要之前不先要基础设施。
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
        case piRelay         = "H 打真 Pi·真帧长（主力）"
        case loadedCall      = "G 真通话节奏（80B）"
        case duplexCall      = "F 边放边录"
        case playbackControl = "A 正对照·只放"
        case duplexQuiet     = "B 双工·不跑麦"
        case duplexLive      = "C 双工·只录不放"
        case syncTrap        = "D 负对照·同步"
        case noAudio         = "E 负对照·无音频"

        var id: String { rawValue }

        /// 这一档预期是什么结果。写在 UI 上，免得测完了还要回来查文档。
        var expectation: String {
            switch self {
            case .piRelay:
                return "🎯 真端点 + 真帧长(1956B×50)—— 就看这档"
            case .loadedCall:
                return "真节奏但只有 80B/帧,是真实量的 1/24"
            case .duplexCall:
                return "✅ 已实测后台能活（但只是闲连接）"
            case .playbackControl: return "应当连上（参考实现验过）"
            case .duplexQuiet, .duplexLive: return "前台能连，后台已实测会断"
            case .syncTrap: return "本该连不上，但 2026-08-27 实测连上了"
            case .noAudio: return "本该连不上，但 2026-08-27 实测连上了"
            }
        }

        /// 要不要跑音频引擎，跑的话放不放声音。
        var engine: (running: Bool, playing: Bool) {
            switch self {
            case .duplexCall, .loadedCall, .piRelay: return (true, true)
            case .duplexLive: return (true, false)
            default: return (false, false)
            }
        }

        /// 发包的节奏与大小。
        ///
        /// ## ⚠ 为什么必须单独测「负载」这一档
        ///
        /// F 档证明的是「**一条闲着的连接**能在后台活下来」—— 它每 2 秒发
        /// 几个字节。而真通话是**每秒 50 个包**，两者在 CPU、无线电唤醒频率、
        /// 发热上完全不是一回事。
        ///
        /// 而 `exceededResourceLimits`（系统因持续高 CPU 收回运行时）恰恰
        /// 是被这些东西触发的，Apple **故意不公布阈值**、明确要求实测。
        /// 拿闲连接的结果去推真通话，等于没测。
        ///
        /// ## 为什么是 4 KB/s 而不是 96 KB/s
        ///
        /// Windows 桥那头是 48kHz 裸 PCM（每方向 97.8 KB/s），**但手表这条腿
        /// 永远不会那样发** —— 电池和无线电都扛不住，一定要压。AAC-ELD
        /// 约 32 kbps ≈ 4 KB/s，那才是真要发的量。
        ///
        /// **50 Hz 这个节奏才是关键变量**（它决定唤醒频率），字节数按实际
        /// 会用的压缩后大小给。照 96 KB/s 打一个免费公共 echo 服务，
        /// 既不礼貌，测的也不是我们真要跑的东西。
        var load: (hertz: Double, payloadBytes: Int) {
            switch self {
            // ⚠ H 档是**真实量**:relay 期望裸 48kHz PCM,1956 字节一帧、
            // 50 帧/秒 = 96 KB/s。这比 G 档大 24 倍,而"能扛住 4 KB/s"
            // 推不出"能扛住 96 KB/s" —— 电池、无线电、发热全是另一回事。
            case .piRelay: return (50, 1956)
            case .loadedCall: return (50, 80)      // 4 KB/s，压缩后的量级
            default: return (0.5, 0)               // 2 秒一个心跳
            }
        }
    }

    enum ProbeError: LocalizedError {
        case microphoneDenied
        case playbackBufferFailed
        var errorDescription: String? {
            switch self {
            case .microphoneDenied:
                // 说清该去哪儿修 —— 手表上没有设置入口，光说"失败"用户没法动。
                return "没有麦克风权限，去手机的 Watch App 里开"
            case .playbackBufferFailed:
                return "建不出播放缓冲（这一档没测成，不是豁免的结论）"
            }
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
    private var player: AVAudioPlayerNode?
    private var beat: Task<Void, Never>?
    private var startedAt: Date?
    private var echoes = 0
    private var sent = 0
    /// 已经进入收尾。收尾时 send 失败是正常的（连接正在被拆），
    /// 必须跟运行中的失败分开记，否则每一轮都会以一条误导性的错误收场。
    private var stopping = false
    private var consecutiveSendErrors = 0
    private var audioActivated = false
    /// 最后一次收到回声的时刻。
    ///
    /// ⚠ 2026-08-27 第四轮实测暴露的洞：息屏再打开后**数字停止变化但没有
    /// 任何报错** —— 因为原来只在 .ready/.waiting/.failed 时更新状态，
    /// 连接若不报错地停止投递，屏幕就永远显示着那个陈旧的「已连上」。
    ///
    /// 这个 app 里本来就有防这个的东西（每屏顶部的 FreshnessBar：
    /// 「宁可让人看见陈旧，也不让陈旧冒充现状」），而探针屏恰恰没加。
    private var lastEchoAt: Date?
    /// 已经报过一次静默，别刷屏。恢复投递时清掉。
    private var stallReported = false
    private var watchdog: Task<Void, Never>?
    /// 整轮里最长的一段没有回声。**这是「它冻住了」唯一能量化的证据。**
    private var longestSilence: TimeInterval = 0
    private var audioObservers: [NSObjectProtocol] = []
    /// 上一次看到的引擎状态。只在**变化**时记日志。
    private var engineWasRunning = false
    /// ⚠ 这个是第二版加的，用来**分辨两种长得一样的失败**：
    /// 「豁免被系统收回」还是「进程被挂起」。TN3135 点名：被拦时 path 恒
    /// `.unsatisfied`。如果息屏后日志里出现 unsatisfied → 是豁免没了；
    /// 如果日志直接断掉、恢复前台才继续 → 是进程被挂起。
    /// 这两件事的修法完全不同，第一版分不出来，等于白测一半。
    private var pathMonitor: NWPathMonitor?
    /// 最近一次看到的网络路径状态。静默发生时要跟引擎状态一起记。
    private var pathSatisfied = true
    /// Pi 在 hello 里下发的 streamId(已解成 16 字节)。**每条连接一个新的。**
    private var streamId: Data?
    /// 这一轮重连了几次。**它本身就是结论**：
    /// 重连能成 = 方案可行,只是需要重连逻辑；
    /// 重连不成 = 才轮到怀疑平台。
    private var reconnects = 0
    /// 上次重连是什么时候。用来在持续静默期间**按节奏重试**,
    /// 而不是只试一次就放弃。
    private var lastReconnectAt: Date?

    /// 公共 echo 服务：A~G 用它，验的是「手表这台设备扛不扛得住」。
    /// ⚠ 它有限流，回声率低不代表 watchOS 有问题。
    private static let echoEndpoint = URL(string: "wss://echo.websocket.org")!
    /// 我们自己的 relay。H 档用它 —— 真端点、真帧长、真鉴权。
    private static let piEndpoint = URL(
        string: "wss://bwicarus.taile44d0c.ts.net:8443/watch-voice")!
    /// 记录层自己的字段,额外信息不许覆盖(见 log 里那段注释)。
    private static let reservedKeys: Set<String> = ["at", "mode", "event", "appState"]

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
        // ⚠ **额外字段不许覆盖保留字段。**
        // 2026-08-27 实测暴露：activateAudio 记 ["mode": "voiceChat"] 当额外
        // 信息,把真正的档位覆盖了,战报里于是冒出一个根本不存在的
        // 「voiceChat」档。**记录层自己把数据弄脏是最难查的一类**,
        // 因为它看上去像真的。
        for (key, value) in extra where !Self.reservedKeys.contains(key) {
            row[key] = value
        }
        // 撞名的额外字段加前缀保留,而不是默默丢掉 —— 丢掉又是一次静默失败。
        for (key, value) in extra where Self.reservedKeys.contains(key) {
            row["x_" + key] = value
        }

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

    /// ⚠ **原因永远优先于序号。**
    ///
    /// 第一版写成「有 `n` 就显示 `n`，否则才显示 `why`」，于是 `send-error`
    /// 两者都有时把**原始错误文本折叠掉了** —— 屏幕上只剩「send-error #3245」，
    /// 而那个数字回答不了任何问题。2026-08-27 实测就卡在这里：三种解释
    /// （对端限流 / 系统掐了 / 只是按停止时的收尾竞态）都活着，分不出来。
    ///
    /// 这是 `silent-failure-lessons.md` 规则二在自己身上犯了一次：
    /// **折成简写之前先报原始值。**
    private static func brief(_ event: String, _ extra: [String: Any]) -> String {
        let stamp = DateFormatter.localizedString(
            from: Date(), dateStyle: .none, timeStyle: .medium)
        let index = (extra["n"] as? Int).map { " #\($0)" } ?? ""
        if let why = extra["why"] as? String {
            return "\(stamp) \(event)\(index) \(why)"
        }
        // ⚠ path 事件的全部价值就在这个状态字上。第五轮实测里屏幕上只显示
        // 了一个光秃秃的「path」,而那一条恰恰是整场最决定性的证据 ——
        // 它出现在最后一个回声的**一秒之后**。
        if let satisfied = extra["satisfied"] as? Bool {
            return "\(stamp) \(event) " + (satisfied ? "✓通" : "✗断")
                + ((extra["status"] as? String).map { " " + $0 } ?? "")
        }
        if let running = extra["engineRunning"] as? Bool {
            return "\(stamp) \(event)\(index) 引擎"
                + (running ? "在跑" : "已停")
        }
        return "\(stamp) \(event)\(index)"
    }

    // ── 开始 ──

    func start(_ picked: Mode) {
        guard case .idle = state else { return }
        mode = picked
        state = .preparing
        echoes = 0
        sent = 0
        stopping = false
        consecutiveSendErrors = 0
        pathSatisfied = true
        streamId = nil
        reconnects = 0
        lastReconnectAt = nil
        lastEchoAt = nil
        stallReported = false
        longestSilence = 0
        engineWasRunning = false
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
        startPathMonitor()
        startAudioObservers()
        startWatchdog()
        openSocket()
    }

    /// 盯住音频会话本身。
    ///
    /// ## ⚠ 为什么这层不能少（2026-08-27 第四轮之后补的）
    ///
    /// 用户报告「息屏再打开后数字停止变化但没有报错」，并怀疑是某种后台
    /// 回收。这个怀疑指向一条我原本完全没观测的链：
    ///
    ///   息屏 → 系统事件中断音频 → **引擎停了** → 活动音频会话没了
    ///        → **豁免随之消失** → 网络悄悄不动了
    ///
    /// **这正好解释了「没有报错」**：网络层根本没出错，是音频先停的。
    /// 而早先调研里有一条对得上 —— 中断发生时 app 在后台的话，系统回收
    /// 音频会激进得多，中断结束后往往无法在后台恢复。
    ///
    /// 少了这层，「冻住了」只是一个现象；有了它，才能变成
    /// 「冻住了**而且当时引擎已经停了**」—— 那才是能定因的证据。
    private func startAudioObservers() {
        stopAudioObservers()
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()

        func observe(_ name: Notification.Name, _ label: String) {
            let token = center.addObserver(
                forName: name, object: session, queue: .main
            ) { [weak self] note in
                Task { @MainActor in
                    var extra: [String: Any] = [:]
                    // 中断类型原样记下来：began / ended 的含义完全不同，
                    // 折成一个布尔就分不出「正在被打断」和「打断结束了」。
                    if let raw = note.userInfo?[
                        AVAudioSessionInterruptionTypeKey] as? UInt {
                        extra["interruptionType"] = raw
                    }
                    if let raw = note.userInfo?[
                        AVAudioSessionRouteChangeReasonKey] as? UInt {
                        extra["routeChangeReason"] = raw
                    }
                    self?.log("audio-" + label, extra)
                }
            }
            audioObservers.append(token)
        }

        observe(AVAudioSession.interruptionNotification, "interruption")
        observe(AVAudioSession.routeChangeNotification, "route-change")
        observe(AVAudioSession.mediaServicesWereResetNotification, "services-reset")
        observe(AVAudioSession.mediaServicesWereLostNotification, "services-lost")
    }

    private func stopAudioObservers() {
        for token in audioObservers {
            NotificationCenter.default.removeObserver(token)
        }
        audioObservers = []
    }

    /// 引擎和播放器现在还在跑吗。
    ///
    /// ⚠ **通知不一定会来**。引擎可能被系统悄悄停掉而不发任何通知 ——
    /// 所以除了监听事件，还要**主动去问**。这跟 silent-failure-lessons
    /// 那条「每个提前退出都要出声」是同一件事：不能只等着别人告诉你。
    private func audioSnapshot() -> [String: Any] {
        [
            "engineRunning": engine?.isRunning ?? false,
            "playerPlaying": player?.isPlaying ?? false,
            "sessionOutputs": AVAudioSession.sharedInstance()
                .currentRoute.outputs.map(\.portName),
        ]
    }

    /// 看门狗：**没有回声也要出声**。
    ///
    /// ⚠ 这是第四轮实测倒逼出来的。用户报告「息屏再打开后数字停止变化但
    /// 没有报错」—— 那正是最该被记下来的时刻，却什么都没记。
    ///
    /// 它跟 `NWPathMonitor` 分工不同：路径监视回答「网络还在不在」，
    /// 看门狗回答「**数据还在不在流动**」。连接可以路径正常、状态还是
    /// `.ready`，但一个字节都不再来 —— 那种情况只有这里看得见。
    ///
    /// ⚠ 它自己也可能被系统连同进程一起挂起。那没关系：那种情况下日志会
    /// **直接断掉**，而「日志断掉」和「日志里写着 stalled」是两个不同的
    /// 结论（进程被挂起 vs 进程活着但连接死了），事后分得开。
    private func startWatchdog() {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                guard let self, !self.stopping else { return }
                guard case .connected = self.state else { continue }
                let since = Date().timeIntervalSince(
                    self.lastEchoAt ?? self.startedAt ?? Date())
                if since > 5, !self.stallReported {
                    self.stallReported = true
                    // ⚠ 静默那一刻的**音频状态**是全场最关键的一条关联证据：
                    // 引擎还在跑 → 豁免应该还在,冻住的是别的东西；
                    // 引擎停了   → 就是它,豁免随音频一起没了。
                    // 两者的修法完全不同,所以必须同时记。
                    var extra: [String: Any] = ["silentSeconds": Int(since)]
                    self.audioSnapshot().forEach { extra[$0.key] = $0.value }
                    // ⚠ 路径状态跟引擎状态**必须并列记**。第五轮实测里
                    // 引擎还在跑(排除了音频),而路径事件恰好出现在静默前
                    // 一秒 —— 只有两个都记下来,才能一眼看出是哪一个。
                    extra["pathSatisfied"] = self.pathSatisfied
                    self.log("stalled", extra)
                    // 静默确认之后试着自己活回来 —— 这才是真正的产品问题：
                    // 不是「能不能撑住」，而是「断了能不能自己恢复」。
                    self.reconnect()
                } else if since > 5, self.stallReported {
                    // ⚠ **还在静默就要接着试。**
                    // 原来只在"首次判定静默"那一刻试一次，失败了就再也不试 ——
                    // 而网络恢复往往需要几秒到几十秒。一次试不成就放弃，
                    // 测出来的是"第一次重连的运气"，不是"能不能恢复"。
                    // 每 6 秒一次，上限由 maximumReconnects 兜住。
                    let sinceRetry = Date().timeIntervalSince(
                        self.lastReconnectAt ?? .distantPast)
                    if sinceRetry > 6 { self.reconnect() }
                }
                // 主动查引擎：它可能被系统停掉而**不发任何通知**。
                // 只在状态**变化**时记,否则会把日志淹掉。
                if self.mode.engine.running {
                    let running = self.engine?.isRunning ?? false
                    if running != self.engineWasRunning {
                        self.engineWasRunning = running
                        self.log(running ? "engine-resumed" : "engine-stopped",
                                 self.audioSnapshot())
                    }
                }
                // 让屏幕上的秒数走起来 —— 陈旧必须看得见。
                self.objectWillChange.send()
            }
        }
    }

    /// 掉线后自己活回来。
    ///
    /// ## ⚠ 只重建 socket，**绝不碰音频会话**
    ///
    /// 这是整个方案能不能成立的关键细节。Apple 框架工程师明说：
    /// 「在 watchOS 上，app 处于后台时**录音无法恢复**，必须由前台的用户
    /// 操作发起。」所以一旦在后台把音频会话停掉或重新激活，就再也起不来了。
    ///
    /// 而网络路径变化（手表息屏时 Wi-Fi 掉了、切到手机中继）是**另一回事**，
    /// 它只需要重建连接。音频会话全程不动，豁免就一直在。
    ///
    /// 2026-08-27 第五轮实测正是这个形态：静默前一秒有 `path` 事件，
    /// 而**引擎还在跑** —— 排除了音频，指向了路径。
    private func reconnect() {
        guard !stopping, reconnects < Self.maximumReconnects else {
            if reconnects >= Self.maximumReconnects {
                log("reconnect-give-up", ["tried": reconnects])
            }
            return
        }
        reconnects += 1
        lastReconnectAt = Date()
        log("reconnect-attempt", ["n": reconnects,
                                  "pathSatisfied": pathSatisfied])
        // 只拆连接。音频引擎、播放器、会话**一律不动**。
        connection?.cancel()
        connection = nil
        consecutiveSendErrors = 0
        openSocket()
    }

    /// 重连上限。不设上限的话，路径长期不通时会变成无限重试 ——
    /// 那既烧电又把日志淹掉，而且掩盖了"它其实一直没回来"这个事实。
    private static let maximumReconnects = 20

    /// 距上次回声多久。屏幕上一直显示它。
    ///
    /// ⚠ 这条存在的唯一理由：**不让陈旧冒充现状**。这个 app 每一屏顶部
    /// 都有同样语义的 FreshnessBar，而探针屏原来没有，于是冻住了也看不出来。
    var silentSeconds: Int? {
        guard case .connected = state else { return nil }
        guard let reference = lastEchoAt ?? startedAt else { return nil }
        return Int(Date().timeIntervalSince(reference))
    }

    /// 监视网络路径。
    ///
    /// ⚠ 这是第二版补的**诊断出口**，它回答第一版回答不了的那个问题：
    /// 息屏之后断掉，到底是「豁免被收回」还是「进程被挂起」？
    ///
    /// - 日志里出现 `path unsatisfied` → **豁免被收回**（TN3135 点名的形态），
    ///   要改的是音频会话怎么维持；
    /// - 日志直接断在息屏那一刻、恢复前台才继续 → **进程被挂起**，
    ///   要改的是后台模式怎么声明。
    ///
    /// 两者的修法完全不同，分不清就只能瞎试。
    private func startPathMonitor() {
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in
                self?.pathSatisfied = path.status == .satisfied
                self?.log("path", [
                    "status": String(describing: path.status),
                    // 被拦时恒 unsatisfied，所以这一位单独拎出来。
                    "satisfied": path.status == .satisfied,
                ])
            }
        }
        monitor.start(queue: .main)
        pathMonitor = monitor
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

        case .duplexQuiet, .duplexLive, .duplexCall, .loadedCall, .piRelay:
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
            let plan = mode.engine
            if plan.running { try startEngine(playing: plan.playing) }

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

    /// 让音频真的流动起来。
    ///
    /// - Parameter playing: 除了录，是否**同时持续播放**一段近静音。
    ///
    /// ## ⚠ 为什么「播放」是单独一个变量（2026-08-27 实测倒逼出来的）
    ///
    /// 第一版探针只装了输入 tap（**只录不放**），实测结果是：前台连得上，
    /// **一息屏或切后台就断**（POSIX 57 Socket is not connected）。
    ///
    /// 而 `UIBackgroundModes: [audio]` 的保活语义是「app **正在播放**音频时
    /// 给额外运行时」—— 只录不放很可能压根不满足它。参考实现那条
    /// `4e322c1 Remove unneeded audio playback` 移除的正是一段持续的近静音
    /// 循环，它敢标 "unneeded" 是**因为那个 demo 只在前台跑**。
    ///
    /// 我们要的恰恰是后台存活，所以那段不是多余的，是必需的。
    ///
    /// ⚠ 音量不能是**真正的**零：系统对完全无声的流有可能不认账。
    /// 用一个听不见但非零的幅度。
    private func startEngine(playing: Bool) throws {
        let made = AVAudioEngine()

        let input = made.inputNode
        input.installTap(onBus: 0, bufferSize: 1024,
                         format: input.outputFormat(forBus: 0)) { _, _ in
            // 刻意什么都不做：这里只是让采集真的在跑。
            // **绝不把音频写进日志或送出去** —— 这是个网络探针，不是录音机。
        }

        if playing {
            let player = AVAudioPlayerNode()
            made.attach(player)
            let format = made.outputNode.inputFormat(forBus: 0)
            made.connect(player, to: made.mainMixerNode, format: format)
            guard let buffer = Self.nearSilentBuffer(format: format) else {
                throw ProbeError.playbackBufferFailed
            }
            try made.start()
            // 循环播放，永不结束 —— 一旦停了，保活的前提就没了。
            player.scheduleBuffer(buffer, at: nil, options: .loops)
            player.play()
            self.player = player
            engine = made
            engineWasRunning = made.isRunning
            log("engine-started", ["playing": true,
                                   "sampleRate": format.sampleRate,
                                   "running": made.isRunning])
            return
        }

        try made.start()
        engine = made
        engineWasRunning = made.isRunning
        log("engine-started", ["playing": false, "running": made.isRunning])
    }

    /// 一段听不见但**不是零**的缓冲。
    ///
    /// 幅度取 1/32768（16 位量化的最小一档）：人耳听不到，但它是真实的样本
    /// 而不是静音，系统不会把它当成"没在播"。
    private static func nearSilentBuffer(format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let frames = AVAudioFrameCount(format.sampleRate)   // 一秒，循环播
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: format, frameCapacity: frames) else { return nil }
        buffer.frameLength = frames
        guard let channels = buffer.floatChannelData else { return nil }
        let amplitude: Float = 1.0 / 32768.0
        for channel in 0..<Int(format.channelCount) {
            let data = channels[channel]
            for frame in 0..<Int(frames) {
                // 交替正负，避免变成直流偏置。
                data[frame] = (frame % 2 == 0) ? amplitude : -amplitude
            }
        }
        return buffer
    }

    // ── WebSocket ──

    private func openSocket() {
        let toPi = mode == .piRelay
        let endpoint = toPi ? Self.piEndpoint : Self.echoEndpoint
        let options = NWProtocolWebSocket.Options()
        options.autoReplyPing = true
        if toPi {
            // ⚠ 取不到 token 就**当场说清**,不要连上去再被 4401 拒 ——
            // 那种失败长得像"平台问题",而实际是"还没配给过"。
            guard let token = WatchTokenStore.load() else {
                log("token-missing")
                state = .failed("还没配语音 token：去「语音」屏点一次配给")
                return
            }
            options.setAdditionalHeaders([("Authorization", "Bearer " + token)])
        }

        let parameters: NWParameters = .tls
        parameters.defaultProtocolStack.applicationProtocols.insert(options, at: 0)
        // TN3135 讲的是给音频流量打标记；这是 Network framework 侧最接近的对应物。
        parameters.serviceClass = .interactiveVoice

        // ⚠ 用 NWConnection 而不是 URLSessionWebSocketTask —— Apple DTS 的 Quinn
        // 点名：watchOS 上「每个 session 都有点像后台 session，实际工作在进程外做」，
        // 所以 URLSession 那条在手表上行为不同，不能拿来判豁免。
        let made = NWConnection(to: .url(endpoint), using: parameters)
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
            // ⚠ **精确匹配，不能用 `contains("50")`。**
            // 原来那么写会把任何含 "50" 的错误文本判成豁免被拒 ——
            // 2026-08-27 实测里屏幕上只出现过 65（No route to host）和
            // 32（Broken pipe），战报却报了「豁免被拒（POSIX 50 签名）」，
            // 多半就是这个子串误报。**折成布尔之前先看清原始值**
            // （silent-failure-lessons 规则二）。
            let denied = why.contains("rawValue: 50")
                || why.contains("Network is down")
                || why.contains("ENETDOWN")
            log("waiting", ["why": why, "isDenial": denied])
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
        connection.receiveMessage { [weak self] data, _, _, error in
            Task { @MainActor in
                guard let self else { return }
                if let error {
                    self.log("receive-error", ["why": String(describing: error)])
                    return
                }
                let now = Date()
                if let previous = self.lastEchoAt {
                    let gap = now.timeIntervalSince(previous)
                    if gap > self.longestSilence { self.longestSilence = gap }
                    // 从静默中恢复了 —— 这条**必须记**：它把「冻住之后又活了」
                    // 和「冻住就再没起来」分开，两者结论完全不同。
                    if self.stallReported {
                        self.stallReported = false
                        self.log("resumed", ["afterSeconds": Int(gap)])
                    }
                }
                self.lastEchoAt = now
                // ⚠ Pi 的 hello 里带着**这条连接专用**的 streamId。
                // 用上一条连接的会被判 FOREIGN 静默丢掉 —— 表现为「连着但
                // 对面听不见」,是这条链路上最难查的一种失败。所以每次
                // 收到 hello 都覆盖,重连之后自然拿到新的。
                if let data, let text = String(data: data, encoding: .utf8),
                   text.hasPrefix("{"),
                   let object = try? JSONSerialization.jsonObject(with: data),
                   let row = object as? [String: Any] {
                    if let stream = row["streamId"] as? String {
                        self.streamId = WatchVoiceWire.streamIdBytes(stream)
                        self.log("stream-id", ["ok": self.streamId != nil,
                                               "raw": stream])
                    }
                    // 服务端的错误原文照抄 —— 它是中文的,本来就写给人看。
                    if let code = row["code"] as? String {
                        self.log("relay-error", ["why": code,
                                                 "message": row["message"] as? String ?? ""])
                    }
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
        let plan = mode.load
        // 每多久发一个包。G 档是 20ms（50 Hz，真通话的节奏）。
        let interval = Duration.nanoseconds(Int(1_000_000_000.0 / plan.hertz))
        // 「多久留一条活着的证据」按时间算而不是按包数算 —— 否则 50 Hz 那档
        // 每 15 个包就写一次文件，光 I/O 就够把结论搅浑了。
        let markEvery = max(1, Int(plan.hertz * 30))
        log("beat-plan", ["hertz": plan.hertz, "payloadBytes": plan.payloadBytes,
                          "bytesPerSecond": Int(plan.hertz) * max(plan.payloadBytes, 16)])

        beat = Task { [weak self] in
            var n = 0
            // G 档的负载：一段固定的假音频帧。内容无所谓，**大小和节奏才是
            // 被测的东西**（唤醒频率决定发热，发热触发 exceededResourceLimits）。
            let filler = plan.payloadBytes > 0
                ? Data(repeating: 0x5A, count: plan.payloadBytes) : Data()

            // ⚠ **按绝对时刻排程，不要 `sleep(for:)` 累加。**
            // 2026-08-27 实测：设 50 Hz 只跑出 16.4 Hz —— `sleep(for:)` 每轮都
            // 从"现在"重新起算，于是循环体的耗时和调度延迟一圈圈累积。
            // 更糟的是它**一声不吭**：屏幕上写着 50 Hz，实际三分之一，
            // 而拿这个数据去推真通话是错的。
            let clock = ContinuousClock()
            var due = clock.now
            var lagWarned = false

            while !Task.isCancelled {
                due = due.advanced(by: interval)
                let now = clock.now
                if due < now {
                    // 追不上了。**说出来**，然后重新对表 —— 不重新对表的话
                    // 后面每次 sleep 都立刻返回，变成烧 CPU 的空转，
                    // 那测的就不是通话负载而是死循环了。
                    let behind = now - due
                    due = now.advanced(by: interval)
                    if let self, !lagWarned {
                        lagWarned = true
                        self.log("pace-lagging", [
                            "targetHertz": plan.hertz,
                            "behindMillis": Int(behind.components.attoseconds
                                                / 1_000_000_000_000_000)
                                + Int(behind.components.seconds) * 1000,
                            "atPacket": n,
                        ])
                    }
                } else {
                    try? await Task.sleep(until: due, clock: clock)
                }
                guard let self else { return }
                // ⚠ **不能写成 `guard let connection … else { return }`。**
                // `return` 退出的是整个 Task —— 而 `reconnect()` 恰恰会把
                // connection 置空一小会儿，于是重连制造的那个空档，把发包
                // 循环**永久杀掉**了。之后 socket 就算接回来，也再没有东西
                // 被发出去，没有回声，于是判成「重连失败」。
                //
                // 2026-08-27 第七轮实测报的「重连 0/1」就是这么来的 ——
                // **我亲手做了个必然失败的实验**：被测的东西被测量装置本身
                // 弄坏了。空档要等下一拍，不是退出。
                guard let connection = self.connection else { continue }
                n += 1

                let data: Data
                if plan.payloadBytes == WatchVoiceWire.frameBytes {
                    // H 档：真帧。⚠ 没拿到 streamId 就**不发** —— 发出去也会被
                    // 判 FOREIGN 静默丢掉,而"发了但对面收不到"比"没发"难查得多。
                    guard let stream = self.streamId else {
                        if n % 50 == 1 { self.log("waiting-stream-id") }
                        continue
                    }
                    // 近静音而不是真零:跟播放那边同一个理由,零流有可能被
                    // 当成"没在发"。内容不重要,大小和节奏才是被测的东西。
                    let payload = Data(repeating: n % 2 == 0 ? 0x01 : 0xFF,
                                       count: WatchVoiceWire.payloadBytes)
                    guard let frame = WatchVoiceWire.encode(
                        streamId: stream,
                        sequence: UInt32(truncatingIfNeeded: n),
                        timestampUs: UInt64(n) * WatchVoiceWire.frameDurationUs,
                        payload: payload)
                    else {
                        self.log("encode-failed", ["n": n])
                        continue
                    }
                    data = frame
                } else if filler.isEmpty {
                    let payload = ["n": n, "appState": Self.appStateName()] as [String: Any]
                    guard let encoded = try? JSONSerialization.data(withJSONObject: payload)
                    else { continue }
                    data = encoded
                } else {
                    data = filler
                }

                let meta = NWProtocolWebSocket.Metadata(
                    opcode: filler.isEmpty ? .text : .binary)
                let context = NWConnection.ContentContext(identifier: "beat", metadata: [meta])
                self.sent = n
                // 只要还有一次成功,就不算"连续"。
                if self.consecutiveSendErrors > 0 && self.lastEchoAt != nil {
                    self.consecutiveSendErrors = 0
                }
                connection.send(content: data, contentContext: context,
                                isComplete: true, completion: .contentProcessed { error in
                    if let error {
                        Task { @MainActor in
                            // 收尾期间的失败单独标记 —— 那是拆连接的正常结果，
                            // 不是「跑着跑着断了」。混在一起会让整轮读不出结论。
                            // ⚠ 连续失败要**熔断**,不能一直往死连接里灌。
                            // 2026-08-27 实测日志里同一个包号连着出现五次,
                            // 就是这么来的:连接死了循环还在跑,每次立刻失败、
                            // 立刻记一条 —— 既刷屏又烧 CPU,还把真正有用的
                            // 第一条错误淹在噪音里。
                            self.consecutiveSendErrors += 1
                            let why = String(describing: error)
                            if self.stopping {
                                self.log("send-error-teardown", ["n": n, "why": why])
                            } else if self.consecutiveSendErrors <= 3 {
                                self.log("send-error", [
                                    "n": n, "why": why,
                                    "consecutive": self.consecutiveSendErrors,
                                    "atSeconds": Int(Date().timeIntervalSince(
                                        self.startedAt ?? Date()))])
                            }
                            // 连着 10 次就认定这条连接没救了,当场收工并**说清楚**。
                            // 静默地继续跑一条死连接,就是这个项目一直在防的那件事。
                            if !self.stopping, self.consecutiveSendErrors == 10 {
                                self.log("give-up", ["after": n, "why": why])
                                self.state = .failed("连续 10 次发送失败：" + why)
                                self.stop()
                            }
                        }
                    }
                })
                // 每 30 秒留一个活着的证据，这样即使一直没断，也能从日志读出
                // 「它撑了多久」而不是只有开头一条。
                if n % markEvery == 0 {
                    self.log("alive", ["n": n, "sentBytes": n * max(data.count, 1)])
                }
            }
        }
    }

    // ── 停止 ──

    func stop() {
        // ⚠ 先立起这面旗再拆 —— 拆连接会让正在飞的 send 失败，
        // 那个失败必须能跟「运行中真的出错」分开。2026-08-27 实测里唯一那条
        // send-error 就出现在最后一个包上，很可能就是这个收尾竞态，
        // 而当时的记录分不出来，于是整轮数据读不出结论。
        stopping = true
        let held = Int(Date().timeIntervalSince(startedAt ?? Date()))
        let hertz = held > 0 ? Double(sent) / Double(held) : 0
        // 收尾时把最后一段静默也算进去 —— 用户往往正是因为"数字不动了"
        // 才来按停止的,那一段恰恰是最该记下来的。
        if let last = lastEchoAt {
            longestSilence = max(longestSilence, Date().timeIntervalSince(last))
        }
        log("stop", ["echoes": echoes, "sent": sent, "heldSeconds": held,
                     "longestSilenceSeconds": Int(longestSilence),
                     "reconnects": reconnects,
                     // 实际达到的速率。⚠ 别假设它等于设定值：Task.sleep 在
                     // 循环里有调度开销，实测只有设定值的三分之一左右。
                     "achievedHertz": (hertz * 10).rounded() / 10])
        state = .stopped(echoes: echoes, heldSeconds: held)
        teardown()
    }

    private func teardown() {
        beat?.cancel()
        beat = nil
        watchdog?.cancel()
        watchdog = nil
        stopAudioObservers()
        pathMonitor?.cancel()
        pathMonitor = nil
        connection?.cancel()
        connection = nil
        player?.stop()
        player = nil
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
        /// 网络路径报过 unsatisfied —— TN3135 说豁免被拒时恒是这个状态。
        /// 有它 = 豁免被收回；没它但日志断掉 = 进程被挂起。**修法不同。**
        var pathLost = false
        var sent = 0
        /// 收到的回声数。⚠ 它是 `Verdict` 自己的字段,不是外层类那个 ——
        /// 战报是从落盘日志重建出来的,跟当前这一轮的运行时状态无关。
        var echoes = 0
        /// 实际达到的发包速率。⚠ 别假设它等于设定值 —— 2026-08-27 实测
        /// 设 50 Hz 只跑出 16.4 Hz（Task.sleep 在循环里的调度开销）。
        /// 真实现要靠**音频 tap 驱动节奏**，它本来就按缓冲区回调。
        var achievedHertz = 0.0
        /// 整轮里最长一段没有回声。**「它冻住了」唯一能量化的证据。**
        var longestSilence = 0
        /// 冻住过几次(看门狗报的),又恢复过几次。
        /// 两个数一起看才知道是"卡一下又好了"还是"卡了就再没起来"。
        var stalls = 0
        var resumes = 0
        /// 音频引擎中途停过。**这是能定因的那一条**：
        /// 冻住时引擎还在跑 = 豁免应该还在,问题在别处；
        /// 冻住时引擎已停   = 就是它,豁免随音频一起没了。
        var engineStopped = false
        /// 收到过音频中断/服务重置这类系统事件。
        var audioEvents = 0
        /// 静默那一刻引擎在不在跑（看门狗随 stalled 一起记的关联证据）。
        var engineRunningAtStall: Bool?
        /// 静默那一刻网络路径断没断。**跟引擎状态并列,谁真谁负责。**
        var pathLostAtStall: Bool?
        /// 重连了几次、成了几次。**这两个数就是最终结论**：
        /// 能重连上 = 方案可行,只是需要重连逻辑;
        /// 重连不上 = 才轮到怀疑平台。
        var reconnectTries = 0
        var reconnectOK = 0
        /// 运行**中**的第一个发送失败（不含收尾时拆连接导致的）。
        var firstErrorAtSeconds: Int?
        var firstErrorWhy: String?

        /// 这一次运行是什么时候开始的（用来当唯一标识 + 显示时刻）。
        var startedAt: Double = 0

        /// ⚠ 必须带上开始时刻：同一档会跑很多次,只用 mode 当 id 会撞,
        /// ForEach 渲染就会错乱 —— 而那种错乱看上去像"数据不对"而不是
        /// "显示不对",很容易把人往错方向带。
        var id: String { mode + "@" + String(Int(startedAt)) }

        /// 一句话结论。刻意分「没测到」和「测到了但是否定的」——
        /// 前者要重测，后者是答案。
        var line: String {
            if micDenied { return "⚠️ 麦克风没权限，这一档没测成" }
            // ⚠ **不要说成「豁免被拒」。** POSIX 50 字面就是 ENETDOWN
            // ——「网络断了」。TN3135 的拒绝会长成这样，**手表息屏时 Wi-Fi
            // 真的掉了也长成这样**，从 app 内部分不开这两者。
            // 2026-08-27 我先写成了「豁免被拒」，那是过度断言：
            // 把一个有两种解释的信号，报成了其中一种。
            if denied {
                return "⚠️ 出现过 ENETDOWN（网络断了）—— 可能是切网，"
                    + "也可能是豁免被收回，**从表内分不开**"
            }
            if !connected { return "❌ 没连上（原因见原始日志）" }
            // ⚠ 运行**中**出错优先报，而且**原文照抄**。第三版之前这里只显示
            // 包序号，于是三种解释（对端限流 / 系统掐了 / 收尾竞态）分不出来，
            // 整轮数据白采。收尾时的失败已在采集侧另记，不会走到这。
            if let at = firstErrorAtSeconds {
                return "🔴 第 \(at)s 起发送失败：\(firstErrorWhy ?? "无原因")"
            }
            // ⚠ 「冻住过」优先于「后台还在跳」——第四轮实测就是这种形态：
            // 前期一切正常，某次息屏之后**数字停止变化但一个错都没报**。
            // 不把它单独拎出来的话，它会被下面那句 ✅ 盖过去。
            // ⚠ **重连成功是压倒一切的好消息**,要排在所有失败叙述之前。
            // 掉线过但每次都自己活回来了 —— 那不是"有问题",那是"能用"。
            if reconnectOK > 0, reconnectOK >= reconnectTries - 1 {
                return "✅ 掉线 \(reconnectTries) 次但**每次都自己重连上了** —— "
                    + "方案可行,要做的只是重连逻辑"
            }
            if reconnectTries > 0, reconnectOK == 0 {
                return "🔴 试了 \(reconnectTries) 次重连**一次都没成** —— "
                    + "这才轮到怀疑平台"
            }
            if stalls > 0 {
                let ending = resumes > 0
                    ? "又恢复 \(resumes) 次" : "**再没恢复**"
                // ⚠ 有引擎状态就先报它 —— 那是能定因的一条,
                // 「冻了几次」只是现象。
                // ⚠ **路径先报,引擎后报。** 第五轮实测:引擎还在跑(排除了
                // 音频),而路径事件出现在最后一个回声的一秒之后 —— 真正的
                // 原因是路径。原来这条判断排在最后,永远走不到,于是屏幕上
                // 只说了"不是音频的锅",没说是什么的锅。
                // **排除了什么** 远不如 **是什么** 有用。
                if pathLostAtStall == true {
                    return "🔵 冻住时**网络路径已经断了** —— 手表切网了，"
                        + "不是平台限制，要做的是重连"
                        + "（\(stalls) 次，最长 \(longestSilence)s，\(ending)）"
                }
                if engineRunningAtStall == false {
                    return "🔴 冻住时**音频引擎已经停了** —— 豁免随音频一起没的"
                        + "（\(stalls) 次，最长 \(longestSilence)s，\(ending)）"
                }
                if engineRunningAtStall == true {
                    return "🟠 冻住时**引擎还在跑** —— 不是音频的锅，问题在别处"
                        + "（\(stalls) 次，最长 \(longestSilence)s，\(ending)）"
                }
                return "🔴 中途冻住 \(stalls) 次（最长静默 \(longestSilence)s），\(ending)"
            }
            if longestSilence > 5 {
                return "🟠 有过 \(longestSilence)s 的静默，但看门狗没来得及记（进程可能被挂起）"
            }
            if beatsWhileBackground > 0 {
                return "✅ 连上了，且**后台仍在跳** \(beatsWhileBackground) 次"
            }
            if heldSeconds < 20 { return "🟡 连上了但很快就停（\(heldSeconds)s），没测到后台" }
            if pathLost {
                return "🔴 连上了但**网络路径被收回**（\(heldSeconds)s）—— 豁免没扛住息屏"
            }
            return "🟠 连上了（\(heldSeconds)s），后台无心跳且路径没报错 —— 更像**进程被挂起**"
        }

        /// 全部信号，一个不落。
        ///
        /// ## ⚠ 为什么不再"挑最重要的那条"
        ///
        /// `line` 只有一行，于是优先级会把别的信号**全挡住**。这个错犯了三次：
        /// - `pathLost` 排在"冻住"之后 → 永远走不到，屏幕上只说了"不是音频的
        ///   锅"，没说**是什么的锅**；
        /// - "豁免被拒"排在最前 → 那一轮到底冻没冻、重连几次，全看不见；
        /// - 更早一次是 `brief()` 里"有序号就不显示原因"。
        ///
        /// 根因是同一个：**一直在替看的人挑重点，而判断需要的是全部信号一起
        /// 看**。所以这一行不挑了，能有的都摆出来 ——
        /// **挑重点是人的事，采集方只负责别丢。**
        var signals: String {
            var parts: [String] = []
            if stalls > 0 { parts.append("冻\(stalls)") }
            if resumes > 0 { parts.append("复\(resumes)") }
            if reconnectTries > 0 {
                parts.append("重连\(reconnectOK)/\(reconnectTries)")
            }
            if pathLost { parts.append("路径断过") }
            if denied { parts.append("ENETDOWN") }
            if engineStopped { parts.append("引擎停过") }
            if audioEvents > 0 { parts.append("音频事件\(audioEvents)") }
            if longestSilence > 0 { parts.append("最长静默\(longestSilence)s") }
            return parts.isEmpty ? "无异常" : parts.joined(separator: " · ")
        }

        /// 发包的实际情况。**跟结论分开显示**，因为它常常解释结论。
        ///
        /// ⚠ 2026-08-27 实测：设 50 Hz 只跑出 16.4 Hz。所以看到「后台活下来了」
        /// 也要看这行 —— 活下来的可能只是三分之一的负载。
        var throughput: String {
            guard sent > 0 else { return "" }
            let ratio = heldSeconds > 0 ? echoes * 100 / max(sent, 1) : 0
            return "发 \(sent) · 实际 \(achievedHertz)Hz · 回声率 \(ratio)%"
        }
    }

    /// 把 jsonl 读成每档一条战报。
    func verdicts() -> [Verdict] {
        guard let data = try? Data(contentsOf: Self.logURL),
              let text = String(data: data, encoding: .utf8) else { return [] }

        // ⚠ **按「运行」分组,不是按「档位」。**
        // 2026-08-27 实测：同一档跑了好几次,战报把它们合并成一条,于是
        // heldSeconds 变成 2970 秒(横跨了没在跑的间隙),而 sent 只来自最后
        // 一次 —— 两个数根本不是同一次运行的,算出来的 Hz 毫无意义。
        // 合并不同次的观测,比不观测更糟:它给出一个看着像真的假数。
        var runs: [Verdict] = []
        var current: Verdict?
        var firstAt: Double = 0
        var lastAt: Double = 0

        for line in text.split(whereSeparator: \.isNewline) {
            guard let row = try? JSONSerialization.jsonObject(
                    with: Data(line.utf8)) as? [String: Any],
                  let mode = row["mode"] as? String,
                  let event = row["event"] as? String else { continue }

            let at = (row["at"] as? Double) ?? 0
            let appState = (row["appState"] as? String) ?? ""

            // 一次 `start` 开一段新运行。上一段就此封存 —— 这样两次运行的
            // 数字永远不会串到一起。
            if event == "start" {
                if var finished = current {
                    finished.heldSeconds = Int(lastAt - firstAt)
                    runs.append(finished)
                }
                var fresh = Verdict(mode: mode)
                fresh.startedAt = at
                current = fresh
                firstAt = at
                lastAt = at
                continue
            }
            // start 之前的孤儿行（比如日志被清过一半）也要有个去处，
            // 否则它们会被默默丢掉。
            if current == nil {
                var orphan = Verdict(mode: mode)
                orphan.startedAt = at
                current = orphan
                firstAt = at
            }
            var verdict = current ?? Verdict(mode: mode)
            lastAt = at

            switch event {
            case "connected":
                // 第一次 connected 是初次连上;之后每一次都是**重连成功**。
                // 分开数,否则"它自己活回来了"这个最关键的事实读不出来。
                if verdict.connected { verdict.reconnectOK += 1 }
                verdict.connected = true
            case "mic-permission":
                if (row["granted"] as? Bool) == false { verdict.micDenied = true }
            case "waiting":
                if (row["isDenial"] as? Bool) == true { verdict.denied = true }
            case "stop":
                verdict.sent = (row["sent"] as? Int) ?? verdict.sent
                verdict.echoes = (row["echoes"] as? Int) ?? verdict.echoes
                verdict.longestSilence =
                    (row["longestSilenceSeconds"] as? Int) ?? verdict.longestSilence
                verdict.achievedHertz =
                    (row["achievedHertz"] as? Double) ?? verdict.achievedHertz
                verdict.reconnectTries =
                    (row["reconnects"] as? Int) ?? verdict.reconnectTries
            case "send-error":
                // ⚠ 只记**第一个**：后面的多半是同一个原因的回响，
                // 而"第几秒开始出错"才是能拿来判断的东西。
                if verdict.firstErrorAtSeconds == nil {
                    verdict.firstErrorAtSeconds = (row["atSeconds"] as? Int) ?? 0
                    verdict.firstErrorWhy = row["why"] as? String
                }
            case "engine-stopped":
                verdict.engineStopped = true
            case "audio-interruption", "audio-services-reset",
                 "audio-services-lost":
                verdict.audioEvents += 1
            case "stalled":
                verdict.stalls += 1
                // ⚠ 只取**第一次**静默时的引擎状态 —— 后面几次多半是同一个
                // 原因的回响,而第一次那一刻才是因果发生的地方。
                if verdict.engineRunningAtStall == nil {
                    verdict.engineRunningAtStall = row["engineRunning"] as? Bool
                    verdict.pathLostAtStall =
                        (row["pathSatisfied"] as? Bool).map { !$0 }
                }
                verdict.longestSilence = max(
                    verdict.longestSilence, (row["silentSeconds"] as? Int) ?? 0)
            case "reconnect-attempt":
                verdict.reconnectTries += 1
            case "resumed":
                verdict.resumes += 1
            case "send-error-teardown":
                break                       // 拆连接的正常结果，不算失败
            case "path":
                // 只认「明确报了不满足」，不把"没记录"当成失去 —— 缺记录
                // 是另一回事（进程被挂起），两者必须分开。
                if (row["satisfied"] as? Bool) == false { verdict.pathLost = true }
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
            current = verdict
        }

        if var last = current {
            last.heldSeconds = Int(lastAt - firstAt)
            runs.append(last)
        }
        // 最近的排最前 —— 屏幕小，先看到刚跑的那次。
        // ⚠ 只留最近 6 次：再多手表上也翻不完，而且旧的那几次多半是
        // 探针自己还没修好时采的，价值更低。
        return Array(runs.reversed().prefix(6))
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
