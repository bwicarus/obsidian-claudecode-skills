import Combine
import Foundation
import WebKit

@MainActor
final class ReaderNativeCommandReceiver: ObservableObject {
    @Published private(set) var notice: String?

    private let store = ReaderNativeBridgeStore()
    private weak var reader: ReaderWebViewModel?
    private weak var voiceBridge: NativeVoiceBridge?
    private var queuedRequestID: String?
    private var processingRequestID: String?
    private var processingTask: Task<Void, Never>?
    private var consumedRequestIDs = Set<String>()

    func bind(
        reader: ReaderWebViewModel,
        voiceBridge: NativeVoiceBridge
    ) {
        self.reader = reader
        self.voiceBridge = voiceBridge
        scheduleIfPossible()
    }

    func receive(_ url: URL) {
        guard let requestID = validatedRequestID(from: url) else {
            notice = "已拒绝无匹配凭据的电脑语音链接"
            return
        }
        guard
            !consumedRequestIDs.contains(requestID),
            processingRequestID != requestID
        else {
            return
        }
        queuedRequestID = requestID
        notice = "正在接收 Safari 的电脑语音请求…"
        scheduleIfPossible()
    }

    func dismissNotice() {
        notice = nil
    }

    private func validatedRequestID(from url: URL) -> String? {
        guard
            url.scheme?.lowercased()
                == ReaderNativeBridgeContract.launchScheme,
            url.host?.lowercased() == "native-voice",
            url.user == nil,
            url.password == nil,
            url.port == nil,
            url.fragment == nil,
            url.path.isEmpty || url.path == "/",
            let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
            ),
            let queryItems = components.queryItems,
            queryItems.count == 1,
            queryItems[0].name == "requestId",
            let requestID = queryItems[0].value,
            ReaderNativeBridgeContract.isSafeRequestID(requestID)
        else {
            return nil
        }
        return requestID
    }

    private func scheduleIfPossible() {
        guard
            processingTask == nil,
            reader != nil,
            voiceBridge != nil,
            let requestID = queuedRequestID
        else {
            return
        }
        queuedRequestID = nil
        processingRequestID = requestID
        processingTask = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.process(requestID: requestID)
            self.processingRequestID = nil
            self.processingTask = nil
            self.scheduleIfPossible()
        }
    }

    private func process(requestID: String) async {
        guard
            let command = await consumeCommandWithBoundedRetry(
                requestID: requestID
            )
        else {
            notice = "Safari 请求未到达或已经过期，请返回 Safari 再点一次"
            return
        }
        consumedRequestIDs.insert(requestID)
        if consumedRequestIDs.count > 64 {
            consumedRequestIDs.removeAll(keepingCapacity: true)
            consumedRequestIDs.insert(requestID)
        }

        guard
            command.contract == ReaderNativeBridgeContract.name,
            command.action == "voice.toggle",
            ReaderNativeBridgeContract.supportedAppKinds.contains(
                command.appKind
            ),
            let appKind = DirectVoiceTargetApp(rawValue: command.appKind),
            let reader,
            let voiceBridge
        else {
            notice = "Safari 电脑语音请求内容无效"
            return
        }

        switch voiceBridge.state.phase {
        case .idle, .failed:
            notice = "正在等待阅读器准备电脑语音…"
            try? store.writeStatus(ReaderNativeVoiceStatus(
                phase: "waiting-reader",
                active: false,
                busy: true,
                sessionID: nil,
                appKind: appKind.rawValue,
                detail: notice
            ))
            guard await reader.waitForNativeVoiceReady() else {
                let message = "阅读器尚未准备好，请返回 Safari 再点一次"
                notice = message
                try? store.writeStatus(ReaderNativeVoiceStatus(
                    phase: "failed",
                    active: false,
                    busy: false,
                    sessionID: nil,
                    appKind: appKind.rawValue,
                    detail: message
                ))
                return
            }
            notice = nil
            await voiceBridge.start(appKind: appKind)

        case .active, .suspended:
            notice = "正在结束电脑语音…"
            await voiceBridge.stop()
            notice = nil

        case .preparing, .connecting, .starting, .stopping:
            notice = "电脑语音正在切换状态，请稍后再点一次"
        }
    }

    private func consumeCommandWithBoundedRetry(
        requestID: String
    ) async -> ReaderNativePendingVoiceCommand? {
        // The URL is opened synchronously from the Safari click while native
        // messaging is asynchronous, so the app may arrive first.
        let deadline = Date().addingTimeInterval(5)
        repeat {
            do {
                if let command = try store.consumePending(
                    requestID: requestID
                ) {
                    return command
                }
            } catch ReaderNativeBridgeStoreError.appGroupUnavailable {
                notice = "BWReader 共享容器不可用"
                return nil
            } catch {
                notice = error.localizedDescription
                return nil
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        } while !Task.isCancelled && Date() < deadline
        return nil
    }
}

extension ReaderWebViewModel {
    /// Read-only readiness probe. It never requests a microphone, opens a WSS,
    /// or hands context ownership away from the web runtime.
    func waitForNativeVoiceReady(
        timeout: TimeInterval = 12
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if webView.url != nil, !isLoading {
                let value = try? await webView.callAsyncJavaScript(
                    """
                    const voice = window.RC && window.RC.computerVoice;
                    return !!voice &&
                      typeof voice.isActive === "function" &&
                      typeof voice.prepareNativeContextHandoff === "function";
                    """,
                    arguments: [:],
                    in: nil,
                    contentWorld: .page
                )
                if value as? Bool == true {
                    return true
                }
            }
            try? await Task.sleep(nanoseconds: 200_000_000)
        } while !Task.isCancelled && Date() < deadline
        return false
    }
}
