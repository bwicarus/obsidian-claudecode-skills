import Combine
import Foundation

@MainActor
final class VoiceSession: ObservableObject {
    enum State: String {
        case idle = "未连接"
        case connecting = "正在连接"
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
    var isStarted: Bool { state == .connecting || state == .preparing || state == .active }

    private let audio = NativeAudio()
    private var socket: URLSessionWebSocketTask?
    private var receiver: Task<Void, Never>?
    private var connectionTimeout: Task<Void, Never>?
    private var audioSender: Task<Void, Never>?
    private var audioQueue: [Data] = []
    private var pendingPlayback: [Data] = []
    private var generation = UUID()

    func start(client: APIClient, deviceID: String, stockCode: String?) async {
        guard !isStarted else { return }
        guard let token = client.token, !token.isEmpty else {
            fail("请先配对此设备。")
            return
        }
        cleanup()
        generation = UUID()
        let current = generation
        state = .connecting
        error = nil
        self.stockCode = stockCode
        sessionID = nil
        threadID = nil
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
            var start: [String: String] = ["type": "start", "clientVersion": "0.2.1",
                                                   "capabilities": "chart.annotation.v1"]
            if let stockCode { start["stockCode"] = stockCode }
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
                self.fail("语音通道初始化超时。请重试。")
            }
        } catch {
            guard current == generation else { return }
            fail("无法建立语音连接：\(error.localizedDescription)")
        }
    }

    func stop() async {
        generation = UUID()
        let oldSocket = socket
        // Stop microphone and detach handlers before awaiting any network operation.
        cleanup(closeSocket: false)
        state = .closed
        if let oldSocket {
            try? await send(["type": "stop"], through: oldSocket)
            oldSocket.cancel(with: .normalClosure, reason: nil)
        }
    }

    func sendText(_ text: String) async {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, isConnected, let socket else { return }
        do { try await send(["type": "text", "text": value], through: socket) }
        catch { fail("文字发送失败：\(error.localizedDescription)") }
    }

    func selectStock(_ code: String) async {
        guard isConnected, stockCode != code, let socket else { return }
        do { try await send(["type": "stock.select", "code": code], through: socket) }
        catch { fail("同步当前股票失败：\(error.localizedDescription)") }
    }

    private func send(_ payload: [String: String], through task: URLSessionWebSocketTask) async throws {
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
                    do { try await handle(JSONDecoder().decode(VoiceEvent.self, from: data), generation: current) }
                    catch { fail("语音消息格式错误：\(error.localizedDescription)"); return }
                @unknown default: break
                }
            }
        } catch {
            guard !Task.isCancelled, current == generation else { return }
            fail("语音已断开：\(error.localizedDescription)")
        }
    }

    private func handle(_ event: VoiceEvent, generation current: UUID) async throws {
        switch event.type {
        case "state":
            if let sessionID = event.sessionId { self.sessionID = sessionID }
            if let threadID = event.threadId { self.threadID = threadID }
            switch event.state {
            case "connecting": state = .connecting
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
                    Task { @MainActor [weak self] in self?.fail("音频被系统中断，请手动重新连接。") }
                }
                do { try audio.start() }
                catch { fail("无法启动音频：\(error.localizedDescription)"); return }
                state = .active
                connectionTimeout?.cancel()
                pendingPlayback.forEach { audio.play($0) }
                pendingPlayback.removeAll()
            case "closed":
                generation = UUID()
                cleanup()
                state = .closed
            default: break
            }
        case "transcript":
            guard let text = event.text, !text.isEmpty else { return }
            let role = event.role ?? "assistant"
            if let index = transcripts.indices.last, !transcripts[index].isFinal, transcripts[index].role == role {
                transcripts[index].text = text
                transcripts[index].isFinal = event.final ?? true
            } else {
                transcripts.append(Transcript(role: role, text: text, isFinal: event.final ?? true))
            }
        case "stock.selected":
            if let code = event.code {
                stockCode = code
                onStockSelected?(code)
            }
        case "capability.action":
            guard let actionID = event.actionId,
                  let capability = event.capability,
                  let operation = event.operation else { throw AppError.message("收到的界面操作缺少必要字段。") }
            let start = event.x.flatMap { x in event.y.map { AnnotationPoint(x: x, y: $0) } }
            let end = event.x2.flatMap { x in event.y2.map { AnnotationPoint(x: x, y: $0) } }
            let action = CapabilityAction(id: actionID, capability: capability, operation: operation,
                                          stockCode: event.code, text: event.text, color: event.color,
                                          start: start, end: end)
            let result = onCapabilityAction?(action) ?? CapabilityResult(success: false, message: "App 尚未注册该界面能力。")
            if let socket {
                try await send(["type": "capability.result", "actionId": actionID,
                                "success": result.success ? "true" : "false", "message": result.message], through: socket)
            }
        case "error": fail(event.message ?? "语音服务器报告错误。")
        default: break
        }
    }

    private func enqueueAudio(_ data: Data, generation current: UUID) {
        guard audioQueue.count < 100 else { fail("语音网络发送过慢，已停止麦克风。请重新连接。"); return }
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
                if current == self.generation { self.fail("语音发送中断：\(error.localizedDescription)") }
            }
        }
    }

    private func fail(_ message: String) {
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
        audioQueue.removeAll()
        pendingPlayback.removeAll()
        if closeSocket { socket?.cancel(with: .goingAway, reason: nil) }
        socket = nil
    }
}
