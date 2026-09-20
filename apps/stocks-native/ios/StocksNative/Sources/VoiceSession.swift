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
    @Published private(set) var processes: [VoiceProcess] = []
    @Published private(set) var diagnostics: [VoiceDiagnosticEntry] = []
    var onStockSelected: ((String) -> Void)?
    var onPlansChanged: ((String?, String?) -> Void)?
    var onCapabilityAction: ((CapabilityAction) -> CapabilityResult)?
    var onSystemCallEnded: ((String) -> Void)?
    private(set) var systemCallID: String?

    var isConnected: Bool { state == .active }
    var isStarted: Bool { state == .connecting || state == .reconnecting || state == .preparing || state == .active }

    var buildVersion: String {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "?"
        return "\(version) (\(build))"
    }
    var socketSummary: String {
        guard let socket else { return "未连接" }
        switch socket.state {
        case .running: return isConnected ? "已连接" : "正在协商"
        case .suspended: return "暂停"
        case .canceling: return "正在关闭"
        case .completed: return "已关闭"
        @unknown default: return "未知"
        }
    }
    var audioSummary: String {
        audio.isRunning ? (systemCallID == nil ? "采集与播放已启动" : "系统来电音频已启动") : "已停止"
    }
    var diagnosticReport: String {
        let formatter = ISO8601DateFormatter()
        let header = "StocksNative \(buildVersion)\n通话=\(state.rawValue) WebSocket=\(socketSummary)\n音频=\(audioSummary) 发送=\(sentPackets) 收到=\(receivedPackets)"
        return ([header] + diagnostics.map {
            "\(formatter.string(from: $0.timestamp)) [\($0.category)] \(VoiceLogPrivacy.clean($0.message))"
        }).joined(separator: "\n")
    }

    func clearDiagnostics() { diagnostics.removeAll() }

    private func recordDiagnostic(_ category: String, _ message: String) {
        diagnostics.append(VoiceDiagnosticEntry(category: category, message: VoiceLogPrivacy.clean(message, limit: 400)))
        if diagnostics.count > 80 { diagnostics.removeFirst(diagnostics.count - 80) }
    }

    private func transcriptTurnID(_ id: String) -> String? {
        let parts = id.split(separator: ":", omittingEmptySubsequences: false)
        if parts.count >= 4 && parts[0] == "backend" { return String(parts[2]) }
        return parts.count == 2 && parts[1] == "assistant" ? String(parts[0]) : nil
    }

    func process(for transcript: Transcript) -> VoiceProcess? {
        guard transcript.role == "assistant", let turnID = transcript.turnID,
              transcripts.last(where: { $0.role == "assistant" && $0.turnID == turnID })?.id == transcript.id else { return nil }
        return processes.first(where: { $0.id == turnID })
    }

    var unattachedProcesses: [VoiceProcess] {
        let attached = Set(transcripts.filter { $0.role == "assistant" }.compactMap(\.turnID))
        return processes.filter { !attached.contains($0.id) }
    }

    private func consumeProcess(_ event: VoiceEvent, restored: Bool = false) {
        guard let id = event.turnId ?? event.requestId else { return }
        if !processes.contains(where: { $0.id == id }) {
            processes.append(VoiceProcess(id: id, requestID: event.requestId))
        }
        guard let index = processes.firstIndex(where: { $0.id == id }) else { return }
        let state = event.state ?? (event.success == true ? "completed" : "failed")
        if event.type == "task" {
            processes[index].state = state
            processes[index].durationMs = event.durationMs ?? processes[index].durationMs
            if let message = event.errorDetail ?? event.message {
                processes[index].error = VoiceLogPrivacy.clean(message)
            }
            if state != "running" {
                for step in processes[index].tools.indices where processes[index].tools[step].state == "running" {
                    processes[index].tools[step].state = "interrupted"
                }
            }
        } else {
            let callID = event.callId ?? "legacy:\(event.name ?? "tool")"
            let step = VoiceToolStep(id: callID, name: VoiceLogPrivacy.clean(event.name ?? "工具", limit: 100),
                                     state: state, durationMs: event.durationMs,
                                     summary: event.summary.map { VoiceLogPrivacy.clean($0) },
                                     error: event.errorDetail.map { VoiceLogPrivacy.clean($0) })
            if let existing = processes[index].tools.firstIndex(where: { $0.id == callID }) {
                if !(processes[index].tools[existing].state != "running" && state == "running") {
                    processes[index].tools[existing] = step
                }
            } else if processes[index].tools.count < 32 {
                processes[index].tools.append(step)
            }
        }
        if !restored {
            recordDiagnostic(event.type == "tool" ? "工具" : "任务",
                             "\(event.name ?? "后台处理") · \(state) · \(event.errorDetail ?? event.message ?? "")")
        }
        if processes.count > 30 { processes.removeFirst(processes.count - 30) }
    }

    private func interruptProcesses() {
        for index in processes.indices where processes[index].state == "running" {
            processes[index].state = "interrupted"
            for step in processes[index].tools.indices where processes[index].tools[step].state == "running" {
                processes[index].tools[step].state = "interrupted"
            }
        }
    }

    private let audio = NativeAudio()
    private let inkUpload = VoiceInkUpload()
    @Published private(set) var inkStatus: String?
    private var socket: URLSessionWebSocketTask?
    private var conversationControl: URLSessionWebSocketTask?
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
    @Published private(set) var newConversationRequested = false

    func start(client: APIClient, deviceID: String, stockCode: String?, systemCallID: String? = nil) async {
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
        self.systemCallID = systemCallID
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
        recordDiagnostic("连接", isReconnect ? "开始恢复原对话" : "开始连接")
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
        recordDiagnostic("音频", "麦克风权限已允许，等待服务器就绪")
        do {
            var request = URLRequest(url: try client.webSocketURL(deviceID: deviceID))
            request.timeoutInterval = 30
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let task = URLSession.shared.webSocketTask(with: request)
            socket = task
            task.resume()
            let clientVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.2.0"
            var start: [String: Any] = ["type": "start", "clientVersion": clientVersion,
                                       "clientTimeZone": TimeZone.current.identifier,
                                       "capabilities": "chart.annotation.v1,ui.context.v1"]
            handshakeStockCode = self.stockCode
            if let handshakeStockCode { start["stockCode"] = handshakeStockCode }
            if let systemCallID { start["callId"] = systemCallID }
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
        recordDiagnostic("连接", "用户结束通话")
        interruptProcesses()
        let endedCall = systemCallID
        systemCallID = nil
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
        if let endedCall { onSystemCallEnded?(endedCall) }
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

    func newConversation(client: APIClient, deviceID: String) async {
        guard !newConversationRequested, !isStarted || isConnected else { return }
        guard let token = client.token, !token.isEmpty else {
            error = "请先登录，再创建新对话。"
            return
        }
        let current = generation
        newConversationRequested = true
        error = nil
        if !isConnected {
            // The existing control route accepts thread.new without starting
            // Codex or requesting microphone access. Wait for its receipt before
            // clearing the visible conversation, including on a fresh launch.
            defer { if current == generation { newConversationRequested = false } }
            do {
                var request = URLRequest(url: try client.webSocketURL(deviceID: deviceID))
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                let control = URLSession.shared.webSocketTask(with: request)
                conversationControl = control
                let timeout = Task {
                    do { try await Task.sleep(nanoseconds: 15_000_000_000) }
                    catch { return }
                    control.cancel(with: .goingAway, reason: nil)
                }
                defer {
                    timeout.cancel()
                    control.cancel(with: .normalClosure, reason: nil)
                    if conversationControl === control { conversationControl = nil }
                }
                control.resume()
                try await send(["type": "thread.new"], through: control)
                while true {
                    let message = try await control.receive()
                    guard current == generation else { return }
                    guard case .string(let text) = message,
                          let data = text.data(using: .utf8) else { continue }
                    let event = try JSONDecoder().decode(VoiceEvent.self, from: data)
                    if event.type == "error" {
                        throw AppError.message(event.message ?? "新对话创建失败。")
                    }
                    if event.type == "state", event.state == "closed", event.reason == "new_thread" {
                        transcripts.removeAll()
                        processes.removeAll()
                        recordDiagnostic("对话", "已创建新对话")
                        threadID = nil
                        sessionID = nil
                        state = .idle
                        return
                    }
                }
            } catch {
                guard current == generation else { return }
                self.error = VoiceLogPrivacy.clean("新对话未获确认：\(error.localizedDescription)；请重试。")
                recordDiagnostic("错误", self.error ?? "新对话未获确认")
            }
            return
        }
        guard let task = socket else {
            newConversationRequested = false
            return
        }
        do {
            connectionTimeout = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: 15_000_000_000) }
                catch { return }
                guard let self, current == self.generation, self.newConversationRequested else { return }
                self.handleConnectionLoss("新对话未获确认，请重新连接后重试。", reason: "reset_timeout")
            }
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
        inkUpload.context(code: context.selectedCode, scope: context.viewState?.inkScopeID)
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

    func updateInk(_ data: Data, stockCode: String, scopeID: String) {
        inkUpload.onStatus = { [weak self] value in self?.inkStatus = value }
        inkUpload.update(data, code: stockCode, scope: scopeID)
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
                    if receivedPackets == 1 { recordDiagnostic("音频", "收到首个下行音频包") }
                    if audio.isRunning { audio.play(data) }
                    else if pendingPlayback.count < 100 { pendingPlayback.append(data) }
                case .string(let text):
                    guard let data = text.data(using: .utf8) else { continue }
                    do {
                        await handle(try JSONDecoder().decode(VoiceEvent.self, from: data), generation: current)
                    } catch {
                        self.error = "收到无法识别的语音消息。"
                        recordDiagnostic("协议", "消息解析失败：\(error.localizedDescription)")
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
            recordDiagnostic("连接", "服务器状态：\(event.state ?? "未知") · \(event.reason ?? "")")
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
                do { try audio.start(managedBySystemCall: systemCallID != nil) }
                catch { fail("无法启动音频：\(error.localizedDescription)"); return }
                state = .active
                recordDiagnostic("音频", audioSummary)
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
                if let client = reconnectClient, let device = reconnectDeviceID, let sessionID {
                    inkUpload.connect(client: client, device: device, session: sessionID)
                }
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
            onPlansChanged?(nil, nil)
            processes.removeAll()
            for item in event.events ?? [] where item.type == "task" || item.type == "tool" {
                consumeProcess(item, restored: true)
            }
            interruptProcesses()
            recordDiagnostic("对话", "已恢复 \(transcripts.count) 条消息、\(processes.count) 个处理过程")
        case "task", "tool":
            consumeProcess(event)
        case "transcript":
            guard let text = event.text, !text.isEmpty else { return }
            reconnectAttempts = 0
            let role = event.role ?? "assistant"
            if let messageID = event.messageId,
               let index = transcripts.firstIndex(where: { $0.id == messageID }) {
                guard transcripts[index].role == role,
                      !(transcripts[index].isFinal && event.final == false) else { return }
                transcripts[index].text = text
                transcripts[index].isFinal = event.final ?? true
                transcripts[index].turnID = event.turnId ?? event.requestId ?? transcripts[index].turnID ?? transcriptTurnID(messageID)
            } else if event.messageId == nil, let index = transcripts.indices.last,
                      !transcripts[index].isFinal, transcripts[index].role == role {
                transcripts[index].text = text
                transcripts[index].isFinal = event.final ?? true
            } else {
                transcripts.append(Transcript(id: event.messageId ?? UUID().uuidString,
                                              role: role, text: text, isFinal: event.final ?? true,
                                              turnID: event.turnId ?? event.requestId ?? event.messageId.flatMap { transcriptTurnID($0) }))
            }
            trimTranscripts()
        case "selection.changed":
            NotificationCenter.default.post(name: .stocksSelectionDidChange, object: nil)
        case "plan.changed":
            onPlansChanged?(event.planId, event.code)
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
            recordDiagnostic("错误", event.message ?? "服务器报告错误")
            if event.fatal == false {
                error = VoiceLogPrivacy.clean(event.message ?? "本次操作未完成，语音连接仍然可用。")
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
            restored.append(Transcript(id: item.id, role: item.role, text: item.text, isFinal: true,
                                       turnID: transcriptTurnID(item.id)))
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
                    if self.sentPackets == 1 { self.recordDiagnostic("音频", "首个上行音频包已发送") }
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
        let message = VoiceLogPrivacy.clean(message)
        recordDiagnostic("连接", "\(reason)：\(message)")
        interruptProcesses()
        generation = UUID()
        cleanup()
        let requestedNewConversation = newConversationRequested
        newConversationRequested = false
        if reason == "new_thread", requestedNewConversation {
            transcripts.removeAll()
            processes.removeAll()
            threadID = nil
            sessionID = nil
        }
        // A terminated system call must never silently reopen a billed session.
        if let callID = systemCallID {
            systemCallID = nil
            wantsConnection = false
            reconnectTask?.cancel()
            reconnectTask = nil
            state = .closed
            error = message
            onSystemCallEnded?(callID)
            return
        }
        if reason == "new_thread", requestedNewConversation, wantsConnection {
            reconnectAttempts = 0
            error = nil
            scheduleReconnect(reason: reason)
            return
        }
        let intentionalReasons: Set<String> = ["idle", "manual", "stop", "new_thread", "reset_timeout"]
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
        recordDiagnostic("重连", "第 \(reconnectAttempts) 次，将在 \(delay) 秒后恢复")
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
        let message = VoiceLogPrivacy.clean(message)
        recordDiagnostic("错误", message)
        interruptProcesses()
        let endedCall = systemCallID
        systemCallID = nil
        wantsConnection = false
        newConversationRequested = false
        reconnectTask?.cancel()
        reconnectTask = nil
        generation = UUID()
        cleanup()
        error = message
        state = .failed
        if let endedCall { onSystemCallEnded?(endedCall) }
    }

    private func cleanup(closeSocket: Bool = true) {
        if audio.isRunning { recordDiagnostic("音频", "停止采集与播放") }
        inkUpload.disconnect()
        conversationControl?.cancel(with: .goingAway, reason: nil)
        conversationControl = nil
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
