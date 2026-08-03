import Combine
import Foundation
import UIKit
import WebKit

@MainActor
final class ReaderNativeCommandReceiver: ObservableObject {
    private enum CommandKind: Equatable {
        case computerVoice
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
            notice = "已拒绝无匹配凭据的电脑语音链接"
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
        notice = received.kind == .computerVoice
            ? "正在接收 Safari 的电脑语音请求…"
            : "正在接收 Safari 的 Realtime 语音请求…"
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
            host == "native-voice" || host == "native-agent",
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
        return (
            requestID,
            host == "native-agent" ? .agentVoice : .computerVoice
        )
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
        if kind == .agentVoice {
            await processAgent(requestID: requestID)
            return
        }
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
            let voiceBridge
        else {
            notice = "Safari 电脑语音请求内容无效"
            return
        }

        switch voiceBridge.state.phase {
        case .idle, .failed:
            guard let webContext = command.webContext,
                  webContext.isValid else {
                notice = "Safari 网页上下文无效，请返回 Safari 再点一次"
                return
            }
            notice = "正在启动电脑语音并交接当前 Safari 网页…"
            try? store.writeStatus(ReaderNativeVoiceStatus(
                phase: "preparing",
                active: false,
                busy: true,
                sessionID: nil,
                appKind: appKind.rawValue,
                detail: notice
            ))
            notice = nil
            await voiceBridge.start(
                appKind: appKind,
                safariWebContext: webContext
            )
            if voiceBridge.state.isActive {
                await returnToSafari(webContext.url)
            }

        case .active, .suspended:
            notice = "正在结束电脑语音…"
            await voiceBridge.stop()
            notice = nil

        case .preparing, .connecting, .starting, .stopping:
            notice = "电脑语音正在切换状态，请稍后再点一次"
        }
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
