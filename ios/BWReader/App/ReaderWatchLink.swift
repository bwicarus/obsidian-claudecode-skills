import Foundation
import UIKit
import WatchConnectivity

/// 手机侧的 WCSession 端 —— 手表的**唯一数据源**。
///
/// 为什么手表不能自己联网见 Shared/ReaderWatchPayload.swift 的文件头：
/// 手表够不到 tailnet，而语音桥与卡片的数据源只在 tailnet 内可达。
///
/// 这个类刻意只做两件事：把手机已经有的状态压成快照送过去、把手表按的
/// 按钮翻译成手机上已有的调用。**它不新增任何业务逻辑** —— 语音怎么开、
/// 待办从哪来、卡片长什么样，都是别处已经定好的事。
@MainActor
final class ReaderWatchLink: NSObject, ObservableObject {
    static let shared = ReaderWatchLink()

    /// 手表按开关时要调的东西。由 App 启动时注入 —— 这个类不自己去找
    /// NativeVoiceBridge，那样会多一条谁也说不清的持有关系。
    var onVoiceStart: (() async -> Void)?
    var onVoiceStop: (() async -> Void)?
    /// 当前语音状态的读取口（同上，注入而不是自己抓）。
    var voiceStatusProvider: (() -> ReaderWatchVoice)?

    private var session: WCSession?
    private var cards: [ReaderWatchCard] = []
    /// 最近一轮说话的结果。随快照一起送 —— 手表锁屏时 sendMessage 到不了，
    /// 靠它在下次打开时补上。
    private var lastVoiceTurn: ReaderWatchTurn?
    private var notifications: [ReaderWatchNotification] = []
    /// 手表屏幕就那么大，留最近 12 张够翻了；更重要的是**载荷有硬上限**，
    /// 超了整份都送不到，而 WCSession 那个错误在手表上根本看不见。
    private let maximumCards = 12

    override private init() { super.init() }

    /// 进程启动时调（含被 WatchConnectivity 后台唤醒那次）。
    ///
    /// 声明成 nonisolated 是为了能在 `App.init()` 里直接调 —— 那个位置不在
    /// MainActor 上。真正的激活仍然回到主线程做。
    nonisolated static func activateFromLaunch() {
        Task { @MainActor in ReaderWatchLink.shared.activate() }
    }

    func activate() {
        guard WCSession.isSupported() else { return }
        guard session == nil else { return }
        let created = WCSession.default
        created.delegate = self
        session = created
        created.activate()
    }

    // ── 手机把东西推给手表 ──

    /// 收到一张新卡片（由 WebView 里的卡片渲染器调过来）。
    func pushCard(_ card: ReaderWatchCard) {
        cards.removeAll { $0.id == card.id }
        cards.insert(card, at: 0)
        if cards.count > maximumCards { cards.removeLast(cards.count - maximumCards) }
        syncToWatch()
    }

    /// JS 侧（rc-voicecall 的卡片渲染器）送来的一张卡片。
    ///
    /// 返回是否真的排给了手表 —— **没送到要说没送到**。静默成功会把
    /// "手表上一直没卡片"变成一个查不出原因的问题。
    func mirror(_ raw: [String: Any]) -> (delivered: Bool, reason: String) {
        guard let id = raw["id"] as? String, !id.isEmpty,
              let kind = raw["kind"] as? String, !kind.isEmpty
        else { return (false, "载荷缺 id 或 kind") }
        let card = ReaderWatchCard(
            id: id,
            kind: kind,
            title: (raw["title"] as? String) ?? "",
            text: (raw["text"] as? String) ?? "",
            receivedAtMs: Date().timeIntervalSince1970 * 1000,
            thumbnailBase64: WatchThumbnail.make(raw["thumbnail"] as? String))
        pushCard(card)
        guard let session, session.activationState == .activated else {
            return (false, "手表会话还没激活")
        }
        guard session.isPaired else { return (false, "没有配对的手表") }
        guard session.isWatchAppInstalled else {
            return (false, "手表上没装 BWReader")
        }
        return (true, "ok")
    }

    func replaceNotifications(_ items: [ReaderWatchNotification]) {
        notifications = items
        syncToWatch()
    }

    /// 语音状态变了就同步一次。
    func voiceStatusChanged() { syncToWatch() }

    private func currentSnapshot() -> ReaderWatchSnapshot {
        ReaderWatchSnapshot(
            generatedAtMs: Date().timeIntervalSince1970 * 1000,
            voice: voiceStatusProvider?() ?? .unknown,
            cards: cards,
            notifications: notifications,
            lastTurn: lastVoiceTurn)
    }

    @discardableResult
    private func syncToWatch() -> Bool {
        guard let session, session.activationState == .activated,
              session.isPaired, session.isWatchAppInstalled
        else { return false }
        guard var payload = WatchSnapshotCoder.encode(currentSnapshot())
        else { return false }
        payload = Self.trimmed(payload, snapshot: currentSnapshot())
        do {
            try session.updateApplicationContext(payload)
            return true
        } catch {
            ReaderWatchLinkDiagnostics.note(
                "updateApplicationContext 失败：\(error.localizedDescription)")
            return false
        }
    }

    /// 载荷超限时**先扔缩略图、再扔老卡片**，而不是整份送不出去。
    ///
    /// ⚠ 这一步存在的理由：applicationContext 有 ~262KB 硬上限，超了整份
    /// 静默丢失 —— 手表上看到的是"一直没更新"，没有任何地方会说为什么。
    /// 宁可少几张图，也不要一份都到不了。
    static func trimmed(
        _ payload: [String: Any], snapshot: ReaderWatchSnapshot
    ) -> [String: Any] {
        func size(_ value: [String: Any]) -> Int {
            (try? JSONSerialization.data(withJSONObject: value).count) ?? 0
        }
        if size(payload) <= ReaderWatchPayload.maximumBytes { return payload }

        var working = snapshot
        // 第一刀：缩略图。文字保底还在，卡片仍然可读。
        working.cards = working.cards.map {
            var card = $0
            card.thumbnailBase64 = nil
            return card
        }
        if let reduced = WatchSnapshotCoder.encode(working),
           size(reduced) <= ReaderWatchPayload.maximumBytes {
            ReaderWatchLinkDiagnostics.note("载荷超限，已去掉缩略图")
            return reduced
        }
        // 第二刀：老卡片，一张张扔到装得下。
        while working.cards.count > 1 {
            working.cards.removeLast()
            if let reduced = WatchSnapshotCoder.encode(working),
               size(reduced) <= ReaderWatchPayload.maximumBytes {
                ReaderWatchLinkDiagnostics.note(
                    "载荷超限，只留最近 \(working.cards.count) 张卡片")
                return reduced
            }
        }
        working.cards = []
        ReaderWatchLinkDiagnostics.note("载荷超限，卡片全部丢弃")
        return WatchSnapshotCoder.encode(working) ?? payload
    }

    // ── 手表按下的按钮 ──

    fileprivate func handle(_ raw: String) async -> [String: Any] {
        let command = ReaderWatchCommand(rawValue: raw)
        // ⚠ 这几个回调只在**视图起来后**才被注入。手机被手表从后台唤醒时
        // 它们全是 nil —— 原来写的是 `await onVoiceStart?()`，nil 就是
        // **静默 no-op**，然后照样回一份快照。手表上表现为「按了、没报错、
        // 什么也没发生」，正是 silent-failure-lessons 规则一说的那种。
        //
        // 而这**不是**能靠改代码绕过去的：启动语音桥要激活麦克风会话，
        // 而 iOS 不允许后台唤醒的进程去激活非混音的 playAndRecord 会话
        // （用户截图里那个 avfaudio 'what' 错误就是撞在这），也没有任何 API
        // 能把手机 app 拉到前台。所以**如实告诉手表**，别假装做了。
        if command == .voiceStart || command == .voiceStop {
            guard onVoiceStart != nil, onVoiceStop != nil else {
                var snapshot = WatchSnapshotCoder.encode(currentSnapshot()) ?? [:]
                snapshot["commandError"] =
                    "手机 App 没在前台，语音桥开关用不了"
                return snapshot
            }
        }
        switch command {
        case .voiceStart:
            await onVoiceStart?()
        case .voiceStop:
            await onVoiceStop?()
        case .refresh, .none:
            break                       // 下面统一回一份最新快照
        }
        return WatchSnapshotCoder.encode(currentSnapshot()) ?? [:]
    }
}

extension ReaderWatchLink: WCSessionDelegate {
    nonisolated func session(
        _ session: WCSession,
        activationDidCompleteWith state: WCSessionActivationState,
        error: Error?
    ) {
        if let error {
            ReaderWatchLinkDiagnostics.note(
                "手表会话激活失败：\(error.localizedDescription)")
        }
        Task { @MainActor in self.syncToWatch() }
    }

    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}

    nonisolated func sessionDidDeactivate(_ session: WCSession) {
        // 换表之后必须重新激活,否则从此再也不同步而且一声不吭。
        WCSession.default.activate()
    }

    /// 手表按住说话录来的一段音频（AAC/m4a）。
    ///
    /// ⚠ **replyHandler 必须立刻回，不能等活干完。**
    /// WCSession 的 reply 有超时（`WCError.messageReplyTimedOut`），而这一轮
    /// 最坏要 105 秒（转写 45s + 问答 60s）—— 在 reply 里同步跑完必然超时，
    /// 而且这跟手机开没开无关。所以：**先回执，再干活，结果另发一条**。
    ///
    /// ⚠ 代理方法跑在后台线程上，而下面第一件事就是切 MainActor —— 让出之后
    /// 进程随时可能被挂起。所以后台断言要在**让出之前**取，不能取在异步中间。
    ///
    /// 走的是 Pi 的回合制链路（/api/voice/transcribe → /api/voice/agent），
    /// 不是电脑上那条常连的语音桥 —— 原因见 ReaderWatchVoiceTurn 的文件头。
    nonisolated func session(
        _ session: WCSession,
        didReceiveMessageData messageData: Data,
        replyHandler: @escaping (Data) -> Void
    ) {
        // 先占住后台时间（这里还在代理线程上，没让出）。
        var assertion = UIBackgroundTaskIdentifier.invalid
        assertion = UIApplication.shared.beginBackgroundTask(
            withName: "watch-voice-turn"
        ) {
            UIApplication.shared.endBackgroundTask(assertion)
            assertion = .invalid
        }

        // 立刻回执。手表据此从"发送中"切到"处理中"，并开始等结果那条消息。
        replyHandler(Self.encode(["accepted": true]))

        Task { @MainActor in
            defer {
                if assertion != .invalid {
                    UIApplication.shared.endBackgroundTask(assertion)
                }
            }
            let cookies = await ReaderWatchVoiceTurn.piCookies()
            var result: [String: Any]
            do {
                let outcome = try await ReaderWatchVoiceTurn.run(
                    clip: messageData, cookies: cookies)
                result = ["heard": outcome.transcript, "reply": outcome.reply]
            } catch {
                let why = (error as? LocalizedError)?.errorDescription
                    ?? error.localizedDescription
                ReaderWatchLinkDiagnostics.note("手表说话失败：" + why)
                result = ["error": why]
            }
            self.deliverTurn(result)
        }
    }

    /// 把这一轮的结果送回手表。
    ///
    /// 两条路都走：`sendMessage` 即时（手表 app 在前台时成立），
    /// `updateApplicationContext` 兜底（手表锁屏/切走了也能在下次打开时看到）。
    /// **不要只走前者** —— 用户抬腕说完就放下手是常态，那时结果会丢，
    /// 而丢了不会有任何地方说。
    private func deliverTurn(_ result: [String: Any]) {
        let turn = ReaderWatchTurn(
            heard: (result["heard"] as? String) ?? "",
            reply: (result["reply"] as? String) ?? "",
            error: result["error"] as? String,
            turnAtMs: Date().timeIntervalSince1970 * 1000)
        lastVoiceTurn = turn
        let session = WCSession.default
        if session.activationState == .activated, session.isReachable,
           let encoded = try? JSONEncoder().encode(turn),
           let object = try? JSONSerialization.jsonObject(with: encoded),
           let dictionary = object as? [String: Any] {
            session.sendMessage(
                ["voiceTurn": dictionary], replyHandler: nil,
                errorHandler: { error in
                    // 到不了很正常（手表锁屏了）—— 快照那条兜底还在，
                    // 所以这里只留痕不报警。
                    ReaderWatchLinkDiagnostics.note(
                        "即时回结果没送到（快照会兜底）：" + error.localizedDescription)
                })
        }
        syncToWatch()
    }

    private static func encode(_ payload: [String: Any]) -> Data {
        (try? JSONSerialization.data(withJSONObject: payload)) ?? Data()
    }

    nonisolated func session(
        _ session: WCSession,
        didReceiveMessage message: [String: Any],
        replyHandler: @escaping ([String: Any]) -> Void
    ) {
        let raw = message[ReaderWatchCommand.key] as? String ?? ""
        Task { @MainActor in
            replyHandler(await self.handle(raw))
        }
    }
}

/// 卡片缩略图：降到手表屏幕真正用得上的尺寸。
///
/// ⚠ 为什么必须降采样：applicationContext 有 ~262KB 硬上限，一张原图就能
/// 把整份载荷顶掉 —— 而超限时整份**静默丢失**，手表上表现为"一直没更新"，
/// 没有任何地方会说为什么。手表 46mm 可用区约 208 点宽，180 点足够了。
enum WatchThumbnail {
    static let maximumEdge: CGFloat = 180
    /// JPEG 质量 0.6：这个尺寸下再高只是白占载荷。
    static let quality: CGFloat = 0.6

    /// 入参是 data: URL（JS 侧只在已经是 data: 时才带过来 —— 手表够不到
    /// tailnet，给它 http 链接取不到）。
    static func make(_ dataURL: String?) -> String? {
        guard let dataURL, dataURL.hasPrefix("data:image/"),
              let comma = dataURL.firstIndex(of: ","),
              let raw = Data(
                base64Encoded: String(dataURL[dataURL.index(after: comma)...])),
              let image = UIImage(data: raw)
        else { return nil }
        let side = max(image.size.width, image.size.height)
        guard side > 0 else { return nil }
        let scale = min(1, maximumEdge / side)
        let target = CGSize(
            width: image.size.width * scale, height: image.size.height * scale)
        let renderer = UIGraphicsImageRenderer(size: target)
        let shrunk = renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        return shrunk.jpegData(compressionQuality: quality)?
            .base64EncodedString()
    }
}

/// 语音状态的人话。手表屏幕小，"connecting" 这种词直接显示没有意义 ——
/// 而且状态文案是**手表上唯一能看懂发生了什么**的地方。
enum ReaderWatchVoicePhrase {
    static func of(_ state: NativeVoiceBridgeState) -> String {
        switch state.phase {
        case .idle: return "未启动"
        case .preparing: return "准备中"
        case .connecting: return "连接中"
        case .starting: return "启动中"
        case .active: return "对话中"
        case .suspended: return "已挂起"
        case .stopping: return "停止中"
        case .failed: return "失败"
        }
    }
}

/// 诊断出口。手表这条链上出事时两端都没有控制台 —— 不留痕就等于不可诊断
/// （references/silent-failure-lessons.md 规则5）。
enum ReaderWatchLinkDiagnostics {
    private static let limit = 40
    private(set) static var entries: [String] = []

    static func note(_ message: String) {
        let stamped = ISO8601DateFormatter().string(from: Date()) + " " + message
        entries.append(stamped)
        if entries.count > limit { entries.removeFirst(entries.count - limit) }
        NSLog("[watch-link] %@", message)
    }
}
