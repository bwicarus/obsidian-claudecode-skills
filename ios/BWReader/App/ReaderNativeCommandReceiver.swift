import Combine
import Foundation
import UIKit
import WebKit

@MainActor
final class ReaderNativeCommandReceiver: ObservableObject {
    // ⚠ 原来还有一个 `.computerVoice`：Safari 上点「电脑语音」→ 拉起 App →
    //   把音频接到 Windows 上某个桌面聊天应用。该功能整个删除了
    //   （2026-09-22 用户拍板），于是这里只剩 Realtime 语音这一种。
    private enum CommandKind: Equatable {
        case agentVoice
    }

    @Published private(set) var notice: String?

    private let store = ReaderNativeBridgeStore()
    private weak var reader: ReaderWebViewModel?
    private weak var voiceBridge: NativeVoiceBridge?
    private var queuedRequestID: String?
    private var queuedKind: CommandKind?
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
        guard let received = validatedRequest(from: url) else {
            notice = "已拒绝无匹配凭据的语音链接"
            return
        }
        let requestID = received.requestID
        guard
            !consumedRequestIDs.contains(requestID),
            processingRequestID != requestID
        else {
            return
        }
        queuedRequestID = requestID
        queuedKind = received.kind
        notice = "正在接收 Safari 的 Realtime 语音请求…"
        scheduleIfPossible()
    }

    func dismissNotice() {
        notice = nil
    }

    private func validatedRequest(
        from url: URL
    ) -> (requestID: String, kind: CommandKind)? {
        let host = url.host?.lowercased()
        guard
            url.scheme?.lowercased()
                == ReaderNativeBridgeContract.launchScheme,
            host == "native-agent",
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
        return (requestID, .agentVoice)
    }

    private func scheduleIfPossible() {
        guard
            processingTask == nil,
            reader != nil,
            voiceBridge != nil,
            let requestID = queuedRequestID,
            let kind = queuedKind
        else {
            return
        }
        queuedRequestID = nil
        queuedKind = nil
        processingRequestID = requestID
        processingTask = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.process(requestID: requestID, kind: kind)
            self.processingRequestID = nil
            self.processingTask = nil
            self.scheduleIfPossible()
        }
    }

    private func process(
        requestID: String,
        kind: CommandKind
    ) async {
        await processAgent(requestID: requestID)
    }

    private func processAgent(requestID: String) async {
        guard
            let command = await consumeAgentCommandWithBoundedRetry(
                requestID: requestID
            ),
            command.contract == ReaderNativeBridgeContract.name,
            command.action == "agent.toggle",
            command.command == "start",
            let webContext = command.webContext,
            webContext.isValid,
            let reader,
            let voiceBridge
        else {
            notice = "Safari Realtime 语音请求内容无效"
            return
        }
        if voiceBridge.state.phase != .idle {
            await voiceBridge.stop()
        }
        notice = "正在启动 Safari Realtime 原生语音…"
        await reader.startExternalNativeAgentVoice(webContext: webContext)
        notice = nil
        await returnToSafari(webContext.url)
    }

    private func returnToSafari(_ rawURL: String) async {
        guard let url = URL(string: rawURL),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return }
        _ = await UIApplication.shared.open(url)
    }

    private func consumeAgentCommandWithBoundedRetry(
        requestID: String
    ) async -> ReaderNativePendingAgentToggle? {
        let deadline = Date().addingTimeInterval(5)
        repeat {
            do {
                if let command = try store.consumePendingAgentToggle(
                    requestID: requestID
                ) {
                    return command
                }
            } catch {
                notice = error.localizedDescription
                return nil
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        } while !Task.isCancelled && Date() < deadline
        return nil
    }
}
