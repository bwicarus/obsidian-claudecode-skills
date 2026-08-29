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

    /// 最近一次拿到的推送 token（十六进制）。上报给 Windows 用。
    private(set) var deviceToken: String?

    /// App 启动时调一次。
    func start() {
        guard registry == nil else { return }
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        self.registry = registry

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
            await ReaderVoipTokenUpload.send(token: hex)
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didInvalidatePushTokenFor type: PKPushType
    ) {
        Task { @MainActor in ReaderVoipCall.shared.deviceToken = nil }
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
        Task { @MainActor in
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
            action.fulfill()
        }
    }

    nonisolated func provider(
        _ provider: CXProvider,
        perform action: CXEndCallAction
    ) {
        // 挂断。⚠ 这**不是**失败，是一次明确的"现在别烦我" ——
        // 上层据此降级到普通通知，而不是重拨。
        Task { @MainActor in
            ReaderVoipCall.shared.currentCallId = nil
            NotificationCenter.default.post(
                name: ReaderVoipCall.declinedNotification, object: nil)
            action.fulfill()
        }
    }

    static let answeredNotification = Notification.Name("bw.voip.answered")
    static let declinedNotification = Notification.Name("bw.voip.declined")
}

/// 把推送 token 送到 Windows —— 没有它，发送方不知道往哪推。
///
/// ⚠ token 会变（重装、恢复备份、系统更新），所以每次拿到都上报，
/// 不做"只报一次"的优化。少报一次的代价是**电话永远打不进来**，
/// 而多报一次只是一个几百字节的请求。
enum ReaderVoipTokenUpload {
    static func send(token: String) async {
        guard let url = ReaderServer.url("/reader-voip/token") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(ReaderServer.origin, forHTTPHeaderField: "Origin")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "token": token,
            "bundleId": Bundle.main.bundleIdentifier ?? "",
            "environment": "production",
        ])
        _ = try? await URLSession.shared.data(for: request)
    }
}
