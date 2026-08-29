import CallKit
import Foundation
import PushKit
import UIKit

/// 「打一通电话过来」—— 通知阶梯最响的那一级（用户 2026-08-29 拍板）。
///
/// ## 它解决什么
///
/// 普通通知会被专注模式、静音、锁屏挡住。当有一条**必须现在让他知道**的事
/// （而他又不在语音会话里、也没在用 App）时，唯一能穿透的就是系统来电 ——
/// 像 LINE / 微信那样。接通之后复用已有的语音通道，AI 直接说。
///
/// ## ⚠ Apple 的硬规矩（不是我们的选择）
///
/// iOS 13 起：**每一个 PushKit VoIP 推送都必须立刻向 CallKit 报一通来电**。
/// 不报的话 App 会被系统杀掉，而且后续 VoIP 推送会被**永久拒发**。
///
/// 后果有两条，都得记住：
/// 1. 这条通道**只能真的响铃**，不能拿来做"更响一点的普通通知"。
///    所以它在 deliver 里是独立的一档，不是别档的加强。
/// 2. `didReceiveIncomingPushWith` 里**不允许有任何提前 return**。
///    哪怕载荷是坏的、哪怕我们判断这条不该响 —— 也必须先报一通，
///    再在下一拍挂掉。下面每一处 return 前都有 reportCall，别删。
///
/// ## ⚠ CallKit 会锁死屏幕
///
/// 通话期间系统通话界面占满屏幕，我们自己的界面**一点都显示不出来**
/// （用户 2026-08 实测）。所以这一级只能"接通后听 AI 说"，
/// 不要计划在通话里展示任何东西。
@MainActor
final class ReaderVoipCall: NSObject {
    static let shared = ReaderVoipCall()

    private var registry: PKPushRegistry?
    private var provider: CXProvider?
    private var currentCallId: UUID?

    /// 开始响铃的时刻 / 接通的时刻。用来分辨"拒接"与"没接" ——
    /// 见 CXEndCallAction 那段。这两个必须在**报来电时**就记，
    /// 不能等到挂断再回推，那时已经没有依据了。
    private var ringingSince: Date?
    private var answeredAt: Date?

    /// 这通电话是为哪条待办打的。AI 发起时经推送载荷带进来，
    /// 挂断时随结局一起回报 —— 没有它，Windows 侧不知道该给谁记账。
    private var currentNotificationId: String?

    /// 给回报用的只读出口（回报发生在挂断之后，那时字段正要被清空）。
    var currentNotificationIdForReport: String? { currentNotificationId }

    /// 最近一次拿到的推送 token（十六进制）。上报给 Windows 用。
    private(set) var deviceToken: String?

    /// 这条链路现在到哪一步了。**iPad 上没有控制台**，不把它显示出来的话
    /// "电话打不进来"就永远只能靠猜（2026-08-29 就卡在这里：token 没上去，
    /// 而"发了被拒"和"根本没发"完全分不开）。显示在「数据与同步」里。
    private(set) var status: String = "还没开始注册"

    /// 记一步。⚠ 覆盖式的，只留最新一句 —— 这不是日志，是"现在什么状态"。
    static func note(_ text: String) async {
        await MainActor.run { ReaderVoipCall.shared.status = text }
    }

    /// App 启动时调一次。
    func start() {
        guard registry == nil else { return }
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        self.registry = registry
        status = "已向系统注册，等 token"

        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallsPerCallGroup = 1
        configuration.maximumCallGroups = 1
        // 只留电话号码类型不行 —— 我们没有号码。generic 允许用任意字符串
        // 当 handle，界面上显示成来电人。
        configuration.supportedHandleTypes = [.generic]
        let provider = CXProvider(configuration: configuration)
        provider.setDelegate(self, queue: .main)
        self.provider = provider
    }

    private var hangupWatch: Task<Void, Never>?

    /// 通话期间盯着"该挂了吗"。
    ///
    /// ⚠ **必须有上限**（这里 5 分钟）：轮询本身不会自己停，而 CallKit 的
    /// 通话可能因为各种原因结束却没走到我们的 EndCall 分支 —— 那样这个
    /// 循环会一直转下去，在后台悄悄耗电，且没有任何症状。
    private func startHangupWatch() {
        hangupWatch?.cancel()
        hangupWatch = Task { @MainActor [weak self] in
            let deadline = Date().addingTimeInterval(5 * 60)
            while !Task.isCancelled, Date() < deadline {
                guard self?.currentCallId != nil else { return }
                if await ReaderVoipCall.shouldHangUp() {
                    self?.endCurrentCall()
                    return
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private static func shouldHangUp() async -> Bool {
        let target = "https://bwicarus-2.taile44d0c.ts.net/reader-voip/hangup"
        guard let url = URL(string: target) else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared
            .data(for: request),
            (response as? HTTPURLResponse)?.statusCode == 200,
            let root = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any]
        else { return false }
        return root["hangup"] as? Bool ?? false
    }

    /// 结束当前通话（AI 说完了，或用户挂断）。
    func endCurrentCall() {
        guard let callId = currentCallId else { return }
        provider?.reportCall(with: callId, endedAt: nil, reason: .remoteEnded)
        currentCallId = nil
    }

    /// 报一通来电。**这是 didReceiveIncomingPush 里唯一允许做的第一件事。**
    private func reportCall(
        title: String,
        completion: @escaping () -> Void
    ) {
        guard let provider else {
            completion()
            return
        }
        let callId = UUID()
        currentCallId = callId
        // ⚠ 这两个必须在**报来电的这一刻**记下。等到挂断再回推就没有依据了 ——
        // 而"拒接还是没接"全靠它们分辨。
        ringingSince = Date()
        answeredAt = nil
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: title)
        update.hasVideo = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsHolding = false
        update.supportsDTMF = false
        provider.reportNewIncomingCall(with: callId, update: update) { _ in
            // ⚠ 报失败也要 completion —— 不调的话系统会认为我们没处理完，
            // 同样按"没报来电"处置。
            completion()
        }
    }
}

extension ReaderVoipCall: PKPushRegistryDelegate {
    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didUpdate pushCredentials: PKPushCredentials,
        for type: PKPushType
    ) {
        let hex = pushCredentials.token
            .map { String(format: "%02x", $0) }
            .joined()
        Task { @MainActor in
            ReaderVoipCall.shared.deviceToken = hex
            ReaderVoipCall.shared.status = "拿到 token，正在上报"
            await ReaderVoipTokenUpload.send(token: hex)
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didInvalidatePushTokenFor type: PKPushType
    ) {
        Task { @MainActor in
            ReaderVoipCall.shared.deviceToken = nil
            // ⚠ 系统主动作废 token 时**必须显示**：这时候电话打不进来，
            // 而设备上不会有任何其它迹象。
            ReaderVoipCall.shared.status = "系统作废了 token（需重新注册）"
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didReceiveIncomingPushWith payload: PKPushPayload,
        for type: PKPushType,
        completion: @escaping () -> Void
    ) {
        // ⚠⚠ 这里**一条提前 return 都不能有**。见类头：不报来电 = App 被杀 +
        // 推送被永久拒发。载荷再离谱也要先响，再决定挂不挂。
        let title = (payload.dictionaryPayload["title"] as? String)
            ?? "BWReader"
        // 这通电话是为哪条待办打的。回报结局时要用 —— 没有它，
        // Windows 侧不知道该给谁记「拒接 / 没接」。
        let ntfId = payload.dictionaryPayload["notificationId"] as? String
        Task { @MainActor in
            ReaderVoipCall.shared.currentNotificationId = ntfId
            ReaderVoipCall.shared.reportCall(title: title, completion: completion)
        }
    }
}

extension ReaderVoipCall: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor in ReaderVoipCall.shared.currentCallId = nil }
    }

    nonisolated func provider(
        _ provider: CXProvider,
        perform action: CXAnswerCallAction
    ) {
        // 接通 —— 把已有的语音通道接起来，AI 那边收到"已接通"就开始说。
        Task { @MainActor in
            NotificationCenter.default.post(
                name: ReaderVoipCall.answeredNotification, object: nil)
            ReaderVoipCall.shared.answeredAt = Date()
            ReaderVoipCall.shared.status = "通话中"
            // AI 说完话会调 voip_push hangup，那边写下信号；这里轮询它。
            // ⚠ 为什么不用推送来挂断：再推一次会**再响一次铃**，
            // 而我们要的是结束当前这通。轮询土但对，通话只有几十秒。
            ReaderVoipCall.shared.startHangupWatch()
            action.fulfill()
        }
    }

    nonisolated func provider(
        _ provider: CXProvider,
        perform action: CXEndCallAction
    ) {
        // ⚠ **「拒接」和「没接」必须分开**（用户 2026-08-29）：
        //
        //   拒接 = 他知道有事找他，只是现在不想接 → 立刻降级，别再打
        //   没接 = 可能没听见               → 隔几分钟再打一次
        //
        // 合成哪一边都是错的：当成拒接会漏掉真的没听见的情况；
        // 当成没接会在他明确说了"别烦我"之后再骚扰一次。
        //
        // CallKit 不直接告诉我们是哪一种 —— EndCall 两种情况都会来。
        // 判据是**有没有先收到 Answer**，以及响了多久：
        // 用户主动按拒接是在铃响期间的一个动作，而系统超时挂断要等
        // 一整个响铃周期（约 30-45 秒）。
        Task { @MainActor in
            let call = ReaderVoipCall.shared
            let outcome: String
            if call.answeredAt != nil {
                outcome = "answered"
            } else if let began = call.ringingSince,
                      Date().timeIntervalSince(began) < 25 {
                outcome = "declined"
            } else {
                outcome = "unanswered"
            }
            call.hangupWatch?.cancel()
            call.hangupWatch = nil
            call.currentCallId = nil
            call.answeredAt = nil
            call.ringingSince = nil
            call.status = "上一通电话：" + outcome
            NotificationCenter.default.post(
                name: ReaderVoipCall.declinedNotification, object: nil)
            // ⚠ 回报**之后**才清 notificationId —— 清早了 Windows 侧
            // 就不知道这个结局属于哪条待办，那条会永远卡在"已拨出"。
            await ReaderVoipOutcomeUpload.send(outcome: outcome)
            call.currentNotificationId = nil
            action.fulfill()
        }
    }

    static let answeredNotification = Notification.Name("bw.voip.answered")
    static let declinedNotification = Notification.Name("bw.voip.declined")
}

/// 回报这通电话的结局。**Windows 侧据此决定重拨还是降级。**
///
/// ⚠ 报不上去的后果是"这条待办卡在'已拨出'状态"：不会重拨、也不会降级，
/// 用户什么也收不到。所以这里跟 token 上报一样 —— **失败必须留痕**。
enum ReaderVoipOutcomeUpload {
    static func send(outcome: String) async {
        let ntfId = await MainActor.run {
            ReaderVoipCall.shared.currentNotificationIdForReport
        }
        let target = "https://bwicarus-2.taile44d0c.ts.net/reader-voip/outcome"
        guard let url = URL(string: target) else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "outcome": outcome,
            "notificationId": ntfId ?? "",
        ])
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            if code != 200 {
                await ReaderVoipCall.note("结局回报被拒 HTTP \(code)")
            }
        } catch {
            await ReaderVoipCall.note(
                "结局回报失败：" + error.localizedDescription)
        }
    }
}

/// 把推送 token 送到 Windows —— 没有它，发送方不知道往哪推。
///
/// ⚠ token 会变（重装、恢复备份、系统更新），所以每次拿到都上报，
/// 不做"只报一次"的优化。少报一次的代价是**电话永远打不进来**，
/// 而多报一次只是一个几百字节的请求。
enum ReaderVoipTokenUpload {
    /// ⚠ **不发 Origin 头。**
    ///
    /// 2026-08-29 实测：小组件用同一个地址、同一台机器打桥是通的，而它
    /// **不发 Origin**；桥那条路由只查 `Tailscale-User-Login`（由
    /// tailscale serve 注入）。我第一版显式发了 `ReaderServer.origin`
    /// （bwicarus-2.…），而桥的白名单里根本没有这个 origin —— 发了反而被拒。
    ///
    /// 结论：跟着**已经被证明能通的那条路**走，不要自己另配一套。
    static func send(token: String) async {
        let target = "https://bwicarus-2.taile44d0c.ts.net/reader-voip/token"
        guard let url = URL(string: target) else {
            await ReaderVoipCall.note("上报地址拼不出来")
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "token": token,
            "bundleId": Bundle.main.bundleIdentifier ?? "",
            "environment": "production",
        ])
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            // ⚠ **成功也要留痕**。第一版这里是 `_ = try? await …`，
            // 把一切吞了 —— 于是 token 没上去时，完全分不清是
            // 「发了被拒」还是「根本没发」，只能靠猜。
            await ReaderVoipCall.note(
                code == 200 ? "已上报（HTTP 200）" : "上报被拒 HTTP \(code)")
        } catch {
            await ReaderVoipCall.note(
                "上报失败：" + error.localizedDescription)
        }
    }
}
