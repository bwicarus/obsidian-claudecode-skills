import Foundation

/// 出事了自己把现场送出去，而不是死在原地。
///
/// ⚠ 它存在的理由（用户 2026-09-22：「不能做一个出问题不立刻退出而是自动发送
/// 故障信息给你的机制么」）：此前每次"崩溃"能拿到的只有一句"点一下就崩"。
/// 页面被杀 → 页面里的日志跟着没了；App 被杀 → 内存里的一切跟着没了。
/// 于是只能靠猜，而我已经猜错过两轮。
///
/// 两条命的设计：
///   ① **面包屑写在 App 进程里**，页面死了它还在 → 渲染进程被回收时当场能报；
///   ② **面包屑同时落盘**，并留一个"这次还没正常退出"的标记 → 连 App 一起被杀
///      时，下次启动读到标记就把上一条命的现场补报出去。
/// 缺哪一条都会有一整类故障永远查不到：只做 ① 抓不到 App 自己崩，
/// 只做 ② 则渲染进程被回收（App 没死）永远等不到"下次启动"。
///
/// 送到哪：Windows 桥的 `/reader-error-log` —— **这条管子早就通了**
/// （`bridgeMirror` 已在用），落到 `%LOCALAPPDATA%\BWReader\error-log.jsonl`。
/// 不新造通道，就是为了不引入"新管子自己也坏了"这一层。
@MainActor
final class ReaderNativeFaultReporter {
    static let shared = ReaderNativeFaultReporter()

    /// 一条面包屑。只留"做了什么"，**不留内容**（选区正文/对话正文都不进来）。
    private struct Crumb: Codable {
        let at: Double
        let kind: String
        let text: String
    }

    private struct Session: Codable {
        let startedAt: Double
        var clean: Bool
        var crumbs: [Crumb]
        /// 还没送出去的报告。⚠ 没有它，"桥没开着/电脑睡了"这一种情况下报告就
        /// 直接蒸发了 —— 而那恰恰是最需要报告的时候（用户在外面用 iPad）。
        /// 发不出去就留着，下次启动再发。
        var outbox: [[String: String]] = []
    }

    /// 上限存在的理由跟别处一样：没有上限的缓冲区在长会话里就是另一个内存问题。
    private let maxCrumbs = 160
    private var crumbs: [Crumb] = []
    private var startedAt = Date().timeIntervalSince1970
    private var pendingFlush: Task<Void, Never>?
    private var origin = ""
    private var outbox: [[String: String]] = []
    private var sending = false
    /// 上一次投递的结果。⚠ 摆在设置里给人看 —— 诊断通道自己哑掉时，
    /// 如果它也不出声，就又回到"什么都没发生"。这次就是这么卡住的：
    /// 837 装上了、崩了，而 Windows 这边一条都没收到，没人说得出为什么。
    private(set) var lastDeliveryNote = "还没有需要上报的故障"

    /// 给「阅读设置 → 诊断」显示的一行。
    var statusLine: String {
        "发件箱 \(outbox.count) 条 · \(lastDeliveryNote)"
    }

    private lazy var stateURL: URL = {
        let base = (FileManager.default.urls(for: .applicationSupportDirectory,
                                             in: .userDomainMask).first
                    ?? FileManager.default.temporaryDirectory)
            .appendingPathComponent("BWReader", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent("fault-session.json")
    }()

    private init() {}

    private var began = false

    /// 启动时调。⚠ **只做一次**：每次回前台都重开的话，上一条命的现场会被
    /// 当场覆盖掉，而那正是唯一的证据。
    /// ⚠ 先读上一条命再写这一条，顺序反了同样是覆盖证据。
    func beginSession(origin: String) {
        self.origin = origin
        guard !began else { persist(clean: false); flushOutbox(); return }
        began = true
        let previous = loadSession()
        startedAt = Date().timeIntervalSince1970
        crumbs = []
        // 上次没发出去的接着发。⚠ 这是"电脑当时睡着了"那一整类情况的唯一出路。
        outbox = previous?.outbox ?? []
        persist(clean: false)
        flushOutbox()
        guard let previous, !previous.clean else { return }
        // 上次没走到 endSession：App 被系统杀了或自己崩了。把那次的现场补报出去。
        let tail = previous.crumbs.suffix(40).map(Self.line).joined(separator: " | ")
        report(code: "BW_APP_UNCLEAN_EXIT",
               message: "上次阅读没有正常退出（App 进程被杀或崩溃）",
               detail: "started=" + Self.stamp(previous.startedAt) + " crumbs=" + tail)
    }

    /// 进后台时调：把这条命标成干净结束。
    /// ⚠ 没有它，"被系统杀掉"和"用户正常切走"看起来一模一样，于是每次启动都误报
    ///   一次 —— 误报多了这条通道就没人看了，等于白做。
    func endSession() { persist(clean: true) }

    /// 记一笔面包屑。要足够便宜 —— 它会被每个原生命令调到。
    func note(_ kind: String, _ text: String) {
        crumbs.append(Crumb(at: Date().timeIntervalSince1970, kind: kind,
                            text: String(text.prefix(120))))
        if crumbs.count > maxCrumbs { crumbs.removeFirst(crumbs.count - maxCrumbs) }
        scheduleFlush()
    }

    /// 报一次故障。⚠ **不阻塞、不抛、失败也不吵** —— 上报本身出问题时最不该做的
    /// 就是再制造一个故障；但它会先落盘，所以即使这次没送出去，下次启动还能补。
    func report(code: String, message: String, detail: String) {
        let crumbTail = crumbs.suffix(30).map(Self.line).joined(separator: " | ")
        let row: [String: String] = [
            "at": Self.stamp(Date().timeIntervalSince1970),
            "source": "app",
            "code": String(code.prefix(120)),
            "message": String(message.prefix(1000)),
            "detail": String((detail + (crumbTail.isEmpty ? "" : " ‖ " + crumbTail)).prefix(1800))
        ]
        note("fault", code)
        outbox.append(row)
        // 上限：发件箱本身不能变成第二个内存问题。满了丢**最旧**的 ——
        // 同一类故障反复发生时，最近那几次才有诊断价值。
        if outbox.count > 40 { outbox.removeFirst(outbox.count - 40) }
        persist(clean: false)
        flushOutbox()
    }

    /// 把发件箱里的报告一条条送出去，送成了才划掉。
    /// ⚠ 失败**不吵**也**不丢**：上报本身出问题时最不该做的是再制造一个故障，
    ///   但更不该做的是假装发过了 —— 那就又回到"什么都没发生"。
    private func flushOutbox() {
        guard !sending, !outbox.isEmpty, !origin.isEmpty,
              let url = URL(string: origin + "/reader-error-log") else { return }
        sending = true
        let batch = outbox
        Task { @MainActor [weak self] in
            var delivered = 0
            for row in batch {
                var request = URLRequest(url: url)
                request.httpMethod = "POST"
                request.timeoutInterval = 6
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.setValue(self?.origin ?? "", forHTTPHeaderField: "Origin")
                request.httpBody = try? JSONSerialization.data(withJSONObject: row)
                do {
                    let (_, reply) = try await URLSession.shared.data(for: request)
                    let status = (reply as? HTTPURLResponse)?.statusCode ?? 0
                    guard (200...299).contains(status) else {
                        self?.lastDeliveryNote = "上次投递被拒：HTTP \(status)"
                        break
                    }
                } catch {
                    self?.lastDeliveryNote = "上次投递失败：" + error.localizedDescription
                    break
                }
                delivered += 1
            }
            guard let self else { return }
            self.sending = false
            if delivered > 0 { self.lastDeliveryNote = "已送出 \(delivered) 条" }
            if delivered > 0 {
                self.outbox.removeFirst(min(delivered, self.outbox.count))
                self.persist(clean: false)
            }
        }
    }

    // MARK: - 落盘

    /// ⚠ 面包屑必须**落盘**，不能只在内存里：App 被系统杀掉时内存里的一切一起没。
    ///   但也不能每条都写盘（一次点按能产生好几条）—— 攒 1.5 秒写一次。
    private func scheduleFlush() {
        guard pendingFlush == nil else { return }
        pendingFlush = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            self?.pendingFlush = nil
            self?.persist(clean: false)
        }
    }

    private func persist(clean: Bool) {
        let session = Session(startedAt: startedAt, clean: clean, crumbs: crumbs, outbox: outbox)
        guard let data = try? JSONEncoder().encode(session) else { return }
        try? data.write(to: stateURL, options: .atomic)
    }

    private func loadSession() -> Session? {
        guard let data = try? Data(contentsOf: stateURL) else { return nil }
        return try? JSONDecoder().decode(Session.self, from: data)
    }

    // MARK: - 小工具

    private static func line(_ crumb: Crumb) -> String {
        stamp(crumb.at) + " " + crumb.kind + ":" + crumb.text
    }

    private static func stamp(_ seconds: Double) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: Date(timeIntervalSince1970: seconds))
    }
}
