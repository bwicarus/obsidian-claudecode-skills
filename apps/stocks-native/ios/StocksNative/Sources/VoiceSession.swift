import Combine
import Foundation

extension Notification.Name {
    static let stocksSelectionDidChange = Notification.Name("stocksNative.selectionDidChange")
}

@MainActor
final class VoiceSession: ObservableObject {
    enum State: String {
        case idle = "未连接"
        case connecting = "正在连接"
        case reconnecting = "正在重连"
        case preparing = "正在启动麦克风"
        case active = "通话中"
        case closed = "已结束"
        case failed = "连接异常"
    }

    @Published private(set) var state: State = .idle
    @Published private(set) var error: String?
    @Published private(set) var transcripts: [Transcript] = []
    @Published private(set) var sessionID: String?
    @Published private(set) var threadID: String?
    @Published private(set) var stockCode: String?
    @Published private(set) var sentPackets = 0
    @Published private(set) var receivedPackets = 0
    var onStockSelected: ((String) -> Void)?
    var onCapabilityAction: ((CapabilityAction) -> CapabilityResult)?

    var isConnected: Bool { state == .active }
    var isStarted: Bool { state == .connecting || state == .reconnecting || state == .preparing || state == .active }

    private let audio = NativeAudio()
    private var socket: URLSessionWebSocketTask?
    private var receiver: Task<Void, Never>?
    private var connectionTimeout: Task<Void, Never>?
    private var audioSender: Task<Void, Never>?
    private var audioQueue: [Data] = []
    private var pendingPlayback: [Data] = []
    private var generation = UUID()
    private var lastContextPayload: String?
    private var pendingContextPayload: String?
    private var pendingContextObject: [String: Any]?
    private var contextFlushTask: Task<Void, Never>?
    private var contextFlushID = UUID()
    private var contextSendInFlight = false
    private var latestContextPayload: String?
    private var latestContextObject: [String: Any]?
    private var reconnectTask: Task<Void, Never>?
    private var stabilityTask: Task<Void, Never>?
    private var reconnectClient: APIClient?
    private var reconnectDeviceID: String?
    private var reconnectAttempts = 0
    private var wantsConnection = false
    private var reconnectExpectedThreadID: String?
    private var handshakeStockCode: String?
    private var newConversationRequested = false

    func start(client: APIClient, deviceID: String, stockCode: String?) async {
        guard !isStarted else { return }
        guard let token = client.token, !token.isEmpty else {
            fail("请先配对此设备。")
            return
        }
        reconnectTask?.cancel()
        reconnectTask = nil
        reconnectClient = client
        reconnectDeviceID = deviceID
        reconnectAttempts = 0
        wantsConnection = true
        reconnectExpectedThreadID = nil
        newConversationRequested = false
        await connect(client: client, deviceID: deviceID, stockCode: stockCode,
                      token: token, isReconnect: false)
    }

    private func connect(client: APIClient, deviceID: String, stockCode: String?,
                         token: String, isReconnect: Bool) async {
        guard wantsConnection else { return }
        cleanup()
        generation = UUID()
        let current = generation
        state = isReconnect ? .reconnecting : .connecting
        if !isReconnect { error = nil }
        self.stockCode = stockCode
        sessionID = nil
        if isReconnect {
            reconnectExpectedThreadID = threadID
        } else {
            threadID = nil
            reconnectExpectedThreadID = nil
        }
        sentPackets = 0
        receivedPackets = 0
        // Request permission before creating a billed remote session; start capture only after ready.
        let permitted = await NativeAudio.requestPermission()
        guard current == generation else { return }
        guard permitted else {
            fail("麦克风权限未开启，请在系统设置中允许股票 App 使用麦克风。")
            return
        }
        do {
            var request = URLRequest(url: try client.webSocketURL(deviceID: deviceID))
            request.timeoutInterval = 30
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let task = URLSession.shared.webSocketTask(with: request)
            socket = task
            task.resume()
            let clientVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.2.0"
            var start: [String: Any] = ["type": "start", "clientVersion": clientVersion,
                                       "capabilities": "chart.annotation.v1,ui.context.v1"]
            handshakeStockCode = self.stockCode
            if let handshakeStockCode { start["stockCode"] = handshakeStockCode }
            try await send(start, through: task)
            guard current == generation else { task.cancel(with: .goingAway, reason: nil); return }
            receiver = Task { [weak self] in
                guard let self else { return }
                await self.receive(from: task, generation: current)
            }
            connectionTimeout = Task { [weak self] in
                // Server allows SDP negotiation (40 s) and realtime readiness (30 s).
                try? await Task.sleep(nanoseconds: 80_000_000_000)
                guard !Task.isCancelled, let self, self.generation == current, self.state != .active else { return }
                self.handleConnectionLoss("语音通道初始化超时，正在重连。", reason: "timeout")
            }
        } catch {
            guard current == generation else { return }
            handleConnectionLoss("无法建立语音连接：\(error.localizedDescription)", reason: "connect")
        }
    }

    func stop() async {
        wantsConnection = false
        reconnectTask?.cancel()
        reconnectTask = nil
        reconnectClient = nil
        reconnectDeviceID = nil
        reconnectExpectedThreadID = nil
        newConversationRequested = false
        generation = UUID()
        let oldSocket = socket
        // Stop microphone and detach handlers before awaiting any network operation.
        cleanup(closeSocket: false)
        state = .closed
        error = nil
        latestContextPayload = nil
        latestContextObject = nil
        pendingContextPayload = nil
        pendingContextObject = nil
        if let oldSocket {
            try? await send(["type": "stop"], through: oldSocket)
            oldSocket.cancel(with: .normalClosure, reason: nil)
        }
    }

    func sendText(_ text: String) async {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, isConnected, let task = socket else { return }
        let current = generation
        do { try await send(["type": "text", "text": value], through: task) }
        catch {
            guard current == generation, socket === task else { return }
            handleConnectionLoss("文字发送时连接中断：\(error.localizedDescription)", reason: "network")
        }
    }

    func newConversation() async {
        guard isConnected, let task = socket else { return }
        let current = generation
        newConversationRequested = true
        error = nil
        do {
            try await send(["type": "thread.new"], through: task)
        } catch {
            guard current == generation, socket === task else { return }
            newConversationRequested = false
            handleConnectionLoss("新对话请求发送中断：\(error.localizedDescription)", reason: "network")
        }
    }

    func selectStock(_ code: String) async {
        guard stockCode != code else { return }
        stockCode = code
        guard isConnected, let task = socket else { return }
        let current = generation
        do { try await send(["type": "stock.select", "code": code], through: task) }
        catch {
            guard current == generation, socket === task else { return }
            handleConnectionLoss("同步当前股票时连接中断：\(error.localizedDescription)", reason: "network")
        }
    }

    func updateContext(_ context: VoiceUIContext) async {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let data = try encoder.encode(context)
            guard data.count <= 8_000, let value = String(data: data, encoding: .utf8),
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            latestContextPayload = value
            latestContextObject = object
            guard value != lastContextPayload else { return }
            if value != pendingContextPayload {
                pendingContextPayload = value
                pendingContextObject = object
            }
            scheduleContextFlush()
        } catch {
            // Context is an optimization. A failed update must not end a working voice call.
        }
    }

    private func scheduleContextFlush(delayNanoseconds: UInt64 = 350_000_000) {
        guard isConnected, socket != nil, pendingContextPayload != nil, !contextSendInFlight else { return }
        contextFlushTask?.cancel()
        let current = generation
        let flushID = UUID()
        contextFlushID = flushID
        contextFlushTask = Task { @MainActor [weak self] in
            if delayNanoseconds > 0 { try? await Task.sleep(nanoseconds: delayNanoseconds) }
            guard !Task.isCancelled, let self, self.contextFlushID == flushID else { return }
            self.contextFlushTask = nil
            self.contextSendInFlight = true
            await self.flushPendingContext(generation: current)
        }
    }

    private func flushPendingContext(generation current: UUID) async {
        guard current == generation, isConnected, let socket,
              let payload = pendingContextPayload, let object = pendingContextObject else { return }
        do {
            try await send(["type": "ui.context", "context": object], through: socket)
            guard current == generation else { return }
            lastContextPayload = payload
            if pendingContextPayload == payload {
                pendingContextPayload = nil
                pendingContextObject = nil
            }
        } catch {
            // Keep the latest unsent snapshot so a later UI change or reconnect can retry it.
            guard current == generation else { return }
        }
        contextSendInFlight = false
        if pendingContextPayload != nil, pendingContextPayload != lastContextPayload {
            if lastContextPayload == payload { scheduleContextFlush() }
        }
    }

    private func send(_ payload: [String: Any], through task: URLSessionWebSocketTask) async throws {
        let data = try JSONSerialization.data(withJSONObject: payload)
        guard let text = String(data: data, encoding: .utf8) else { throw AppError.message("无法编码语音指令。") }
        try await task.send(.string(text))
    }

    private func receive(from task: URLSessionWebSocketTask, generation current: UUID) async {
        do {
            while !Task.isCancelled, current == generation {
                let message = try await task.receive()
                guard current == generation else { return }
                switch message {
                case .data(let data):
                    receivedPackets += 1
                    if audio.isRunning { audio.play(data) }
                    else if pendingPlayback.count < 100 { pendingPlayback.append(data) }
                case .string(let text):
                    guard let data = text.data(using: .utf8) else { continue }
                    do {
                        await handle(try JSONDecoder().decode(VoiceEvent.self, from: data), generation: current)
                    } catch {
                        self.error = "收到无法识别的语音消息：\(error.localizedDescription)"
                    }
                @unknown default: break
                }
            }
        } catch {
            guard !Task.isCancelled, current == generation else { return }
            handleConnectionLoss("语音已断开：\(error.localizedDescription)", reason: "network")
        }
    }

    private func handle(_ event: VoiceEvent, generation current: UUID) async {
        switch event.type {
        case "state":
            if let sessionID = event.sessionId { self.sessionID = sessionID }
            if let threadID = event.threadId {
                if let expected = reconnectExpectedThreadID, expected != threadID {
                    error = "原对话线程无法恢复，已自动开启新的对话。"
                }
                self.threadID = threadID
            }
            switch event.state {
            case "connecting":
                if state != .reconnecting { state = .connecting }
            case "ready", "active":
                guard !audio.isRunning, current == generation else { return }
                state = .preparing
                audio.onPCM = { [weak self] data in
                    Task { @MainActor [weak self] in
                        guard let self, self.generation == current, self.isConnected else { return }
                        self.enqueueAudio(data, generation: current)
                    }
                }
                audio.onInterrupted = { [weak self] in
                    Task { @MainActor [weak self] in
                        guard let self, self.generation == current else { return }
                        self.handleConnectionLoss("音频被系统中断，正在恢复通话。", reason: "audio")
                    }
                }
                do { try audio.start() }
                catch { fail("无法启动音频：\(error.localizedDescription)"); return }
                state = .active
                if reconnectExpectedThreadID == nil || reconnectExpectedThreadID == threadID {
                    error = nil
                }
                reconnectExpectedThreadID = nil
                connectionTimeout?.cancel()
                if stockCode != handshakeStockCode, let code = stockCode, let task = socket {
                    do {
                        try await send(["type": "stock.select", "code": code], through: task)
                        handshakeStockCode = code
                    } catch {
                        guard current == generation, socket === task else { return }
                        handleConnectionLoss("同步当前股票时连接中断：\(error.localizedDescription)", reason: "network")
                        return
                    }
                }
                stabilityTask?.cancel()
                stabilityTask = Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: 20_000_000_000)
                    guard !Task.isCancelled, let self,
                          self.generation == current, self.state == .active else { return }
                    self.reconnectAttempts = 0
                }
                pendingPlayback.forEach { audio.play($0) }
                pendingPlayback.removeAll()
                scheduleContextFlush(delayNanoseconds: 0)
            case "closed":
                let closeReason = event.reason ?? "remote"
                let message = closeReason == "idle"
                    ? "通话已因长时间无人使用而自动结束。"
                    : "语音连接已关闭。"
                handleConnectionLoss(message, reason: closeReason)
            default: break
            }
        case "history":
            replaceHistory(event.items ?? [])
        case "transcript":
            guard let text = event.text, !text.isEmpty else { return }
            reconnectAttempts = 0
            let role = event.role ?? "assistant"
            if let messageID = event.messageId,
               let index = transcripts.firstIndex(where: { $0.id == messageID }) {
                transcripts[index].text = text
                transcripts[index].isFinal = event.final ?? true
            } else if let index = transcripts.indices.last,
                      !transcripts[index].isFinal, transcripts[index].role == role {
                if let messageID = event.messageId { transcripts[index].id = messageID }
                transcripts[index].text = text
                transcripts[index].isFinal = event.final ?? true
            } else {
                transcripts.append(Transcript(id: event.messageId ?? UUID().uuidString,
                                              role: role, text: text, isFinal: event.final ?? true))
            }
            trimTranscripts()
        case "selection.changed":
            NotificationCenter.default.post(name: .stocksSelectionDidChange, object: nil)
        case "stock.selected":
            if let code = event.code {
                stockCode = code
                onStockSelected?(code)
            }
        case "capability.action":
            guard let actionID = event.actionId,
                  let capability = event.capability,
                  let operation = event.operation else {
                error = "收到的界面操作缺少必要字段。"
                return
            }
            let start = event.x.flatMap { x in event.y.map { AnnotationPoint(x: x, y: $0) } }
            let end = event.x2.flatMap { x in event.y2.map { AnnotationPoint(x: x, y: $0) } }
            let action = CapabilityAction(id: actionID, capability: capability, operation: operation,
                                          stockCode: event.code, text: event.text, color: event.color,
                                          start: start, end: end)
            let result = onCapabilityAction?(action) ?? CapabilityResult(success: false, message: "App 尚未注册该界面能力。")
            if let task = socket {
                do {
                    try await send(["type": "capability.result", "actionId": actionID,
                                    "success": result.success ? "true" : "false", "message": result.message], through: task)
                } catch {
                    guard current == generation, socket === task else { return }
                    handleConnectionLoss("界面操作回执发送中断：\(error.localizedDescription)", reason: "network")
                }
            }
        case "error":
            if event.fatal == false {
                error = event.message ?? "本次操作未完成，语音连接仍然可用。"
            } else {
                handleConnectionLoss(event.message ?? "语音服务器报告错误。", reason: "server")
            }
        default: break
        }
    }

    private func replaceHistory(_ items: [VoiceHistoryItem]) {
        var seen = Set<String>()
        var restored: [Transcript] = []
        for item in items.suffix(100) where !item.text.isEmpty {
            guard seen.insert(item.id).inserted else { continue }
            restored.append(Transcript(id: item.id, role: item.role, text: item.text, isFinal: true))
        }
        transcripts = restored
    }

    private func trimTranscripts() {
        if transcripts.count > 100 {
            transcripts.removeFirst(transcripts.count - 100)
        }
    }

    private func enqueueAudio(_ data: Data, generation current: UUID) {
        guard audioQueue.count < 100 else {
            handleConnectionLoss("语音网络发送过慢，正在恢复通话。", reason: "network")
            return
        }
        audioQueue.append(data)
        guard audioSender == nil else { return }
        audioSender = Task { [weak self] in
            guard let self else { return }
            do {
                while !Task.isCancelled, current == self.generation, !self.audioQueue.isEmpty, let socket = self.socket {
                    let packet = self.audioQueue.removeFirst()
                    try await socket.send(.data(packet))
                    self.sentPackets += 1
                }
                if current == self.generation { self.audioSender = nil }
            } catch {
                if current == self.generation {
                    self.handleConnectionLoss("语音发送中断：\(error.localizedDescription)", reason: "network")
                }
            }
        }
    }

    private func handleConnectionLoss(_ message: String, reason: String) {
        generation = UUID()
        cleanup()
        if reason == "new_thread", newConversationRequested, wantsConnection {
            newConversationRequested = false
            transcripts.removeAll()
            threadID = nil
            sessionID = nil
            reconnectAttempts = 0
            error = nil
            scheduleReconnect(reason: reason)
            return
        }
        let intentionalReasons: Set<String> = ["idle", "manual", "stop", "new_thread"]
        if intentionalReasons.contains(reason) || !wantsConnection {
            wantsConnection = false
            reconnectTask?.cancel()
            reconnectTask = nil
            state = .closed
            error = message
            return
        }
        error = message
        scheduleReconnect(reason: reason)
    }

    private func scheduleReconnect(reason _: String) {
        guard wantsConnection, let client = reconnectClient,
              let deviceID = reconnectDeviceID, let token = client.token, !token.isEmpty else {
            fail("语音连接已中断，请手动重新连接。")
            return
        }
        guard reconnectAttempts < 20 else {
            fail("语音多次重连仍未成功，请检查网络后手动重试。")
            return
        }
        let delay = min(30, 2 * (1 << min(reconnectAttempts, 4)))
        reconnectAttempts += 1
        state = .reconnecting
        reconnectTask?.cancel()
        reconnectTask = Task { @MainActor [weak self] in
            do { try await Task.sleep(nanoseconds: UInt64(delay) * 1_000_000_000) }
            catch { return }
            guard let self, self.wantsConnection else { return }
            self.reconnectTask = nil
            await self.connect(client: client, deviceID: deviceID, stockCode: self.stockCode,
                               token: token, isReconnect: true)
        }
    }

    private func fail(_ message: String) {
        wantsConnection = false
        reconnectTask?.cancel()
        reconnectTask = nil
        generation = UUID()
        cleanup()
        error = message
        state = .failed
    }

    private func cleanup(closeSocket: Bool = true) {
        audio.onPCM = nil
        audio.onInterrupted = nil
        audio.stop()
        receiver?.cancel()
        receiver = nil
        connectionTimeout?.cancel()
        connectionTimeout = nil
        audioSender?.cancel()
        audioSender = nil
        stabilityTask?.cancel()
        stabilityTask = nil
        audioQueue.removeAll()
        pendingPlayback.removeAll()
        lastContextPayload = nil
        pendingContextPayload = latestContextPayload
        pendingContextObject = latestContextObject
        contextFlushTask?.cancel()
        contextFlushTask = nil
        contextFlushID = UUID()
        contextSendInFlight = false
        if closeSocket { socket?.cancel(with: .goingAway, reason: nil) }
        socket = nil
    }
}
