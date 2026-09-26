import Foundation

actor DirectVoiceSocket {
    typealias EventHandler = @Sendable (DirectVoiceEvent) -> Void

    private struct PendingRequest {
        let action: String
        let continuation: CheckedContinuation<DirectJSONValue, Error>
        let timeoutTask: Task<Void, Never>
    }

    private let configuration: DirectVoiceConfiguration
    private let eventHandler: EventHandler
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    private var urlSession: URLSession?
    private var webSocket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var heartbeatTask: Task<Void, Never>?
    private var pending: [String: PendingRequest] = [:]
    private var state: DirectVoiceState = .disconnected
    private var intentionalClose = false
    private var contextSessionID: String?

    private var activeSession: DirectVoiceSession?
    private var activeSessionBytes: Data?
    private var heartbeatSequence: UInt32 = 0
    private var uplinkSequence: UInt32 = 0
    private var uplinkTimestampBase: UInt64 = 0
    private var uplinkSendTail: Task<Void, Error>?
    private var uplinkSendGeneration: UInt64 = 0
    private var downlinkNextSequence: UInt32 = 0
    /// 迁出 3b-2：后台快照会话登记了阅读器来源后，桥发来的查询 / 截图 / 输出事件交给它回答。
    private var readerEventHandler: (@Sendable (String, DirectJSONValue) -> Void)?
    private var downlinkLastTimestamp: UInt64?

    init(
        configuration: DirectVoiceConfiguration = .production,
        eventHandler: @escaping EventHandler
    ) {
        self.configuration = configuration
        self.eventHandler = eventHandler
    }

    func currentState() -> DirectVoiceState {
        state
    }

    /// A dedicated data socket never acquires microphone/audio ownership.
    /// The same strict request correlation, size limits and timeout handling
    /// used by voice apply to native replication and other Reader data calls.
    func openReaderContext() async throws -> String {
        guard configuration == .readerContext else { throw failure("BW_READER_CONTEXT_ENDPOINT","数据请求需要独立连接",retryable:false) }
        if let contextSessionID, state == .ready { return contextSessionID }
        try await connect()
        let sessionID = makeSession().id
        let result = try await request(action:"context-open",fields:["sessionId":.string(sessionID)],timeoutNanoseconds:DirectVoiceProtocol.requestTimeoutNanoseconds)
        let object = try requireObject(result,label:"CONTEXT-OPEN")
        try object.requireExactKeys(["sessionId","state","mode"])
        guard try object.requireString("sessionId",maximum:160) == sessionID,
              try object.requireString("state",maximum:32) == "context-only",
              try object.requireString("mode",maximum:32) == "snapshot-mcp" else {
            throw failure("BW_READER_CONTEXT_RESPONSE","数据连接身份未确认",retryable:false)
        }
        contextSessionID = sessionID
        return sessionID
    }

    func requestReaderData(action: String, fields: [String:DirectJSONValue]) async throws -> DirectJSONValue {
        guard configuration == .readerContext, state == .ready, let sessionID = contextSessionID,
              ["replication-command","replication-command-chunk"].contains(action), fields["sessionId"] == nil else {
            throw failure("BW_READER_CONTEXT_REQUEST","数据连接请求未授权或已失效",retryable:false)
        }
        var input = fields; input["sessionId"] = .string(sessionID)
        return try await request(action:action,fields:input,timeoutNanoseconds:DirectVoiceProtocol.requestTimeoutNanoseconds)
    }

    /// 迁出 3b（2026-09-26）：后台通话期间原生持有的阅读器快照会话只做上下文上行 ——
    /// 当前页正文（context）与阅读状态（active-reading），与网页快照链接同一合同。
    func publishReaderContext(action: String, fields: [String: DirectJSONValue]) async throws -> DirectJSONValue {
        guard configuration == .readerContext, state == .ready, let sessionID = contextSessionID,
              ["context", "active-reading"].contains(action), fields["sessionId"] == nil else {
            throw failure("BW_READER_CONTEXT_REQUEST", "快照上行请求未授权或连接已失效", retryable: false)
        }
        var input = fields; input["sessionId"] = .string(sessionID)
        let reply = try await request(action: action, fields: input, timeoutNanoseconds: DirectVoiceProtocol.requestTimeoutNanoseconds)
        let object = try requireObject(reply, label: action == "context" ? "CONTEXT" : "ACTIVE-READING")
        guard try object.requireString("sessionId", maximum: 160) == sessionID,
              ["accepted", "duplicate"].contains(try object.requireString("outcome", maximum: 32)) else {
            throw failure("BW_READER_CONTEXT_ACK", "Windows 快照上行回执无效", retryable: false)
        }
        return reply
    }

    func setReaderEventHandler(_ handler: (@Sendable (String, DirectJSONValue) -> Void)?) {
        readerEventHandler = configuration == .readerContext ? handler : nil
    }

    /// 迁出 3b-2：后台快照会话对桥的应答 —— 登记来源、回查询、回截图、回输出/结果回执。
    /// 与网页快照链接同一组动作与字段；`reader-result-ack` 按原合同不带 sessionId。
    func replyReader(action: String, fields: [String: DirectJSONValue], includeSession: Bool) async throws -> DirectJSONValue {
        guard configuration == .readerContext, state == .ready, let sessionID = contextSessionID,
              ["visual-register", "reader-query", "reader-visual", "reader-realtime-output-ack", "reader-result-ack"].contains(action),
              fields["sessionId"] == nil else {
            throw failure("BW_READER_CONTEXT_REQUEST", "快照应答未授权或连接已失效", retryable: false)
        }
        var input = fields
        if includeSession { input["sessionId"] = .string(sessionID) }
        return try await request(action: action, fields: input, timeoutNanoseconds: DirectVoiceProtocol.requestTimeoutNanoseconds)
    }

    /// One export uses its own context socket; long Anki operations cannot
    /// occupy the microphone or the reading-state replication receive loop.
    func requestReaderAnki(fields: [String: DirectJSONValue]) async throws -> DirectJSONValue {
        guard configuration == .readerContext, state == .ready, let sessionID = contextSessionID,
              fields["sessionId"] == nil else { throw failure("BW_READER_CONTEXT_REQUEST", "Anki 数据连接未就绪", retryable: false) }
        var input = fields; input["sessionId"] = .string(sessionID)
        return try await request(action: "anki-add-cards-local", fields: input, timeoutNanoseconds: 45_000_000_000)
    }

    /// Dictionary inference gets a short-lived data connection of its own.
    /// It must never queue behind or block the real-time audio receive loop.
    func requestReaderDictionary(term: String, context: String, reading: String) async throws -> DirectJSONValue {
        guard configuration == .readerContext, state == .ready, contextSessionID != nil,
              !term.isEmpty, term.utf16.count <= 256, context.utf16.count <= 1200, reading.utf16.count <= 256,
              !(term + context + reading).contains("\0") else {
            throw failure("BW_READER_DICTIONARY_REQUEST_INVALID", "词典数据连接或查询无效", retryable: false)
        }
        // This existing command does not take a sessionId in its payload.
        let reply = try await request(action: "dictionary-lookup", fields: ["mode": .string("meaning"),
            "term": .string(term), "context": .string(context), "reading": .string(reading), "english": .string("")],
            timeoutNanoseconds: 70_000_000_000)
        let result = try requireObject(reply, label: "DICTIONARY")
        try result.requireExactKeys(["term", "mode", "language", "text", "source", "cached"])
        guard try result.requireString("term", maximum: 256) == term,
              try result.requireString("mode", maximum: 16) == "meaning",
              try result.requireString("language", maximum: 16) == "zh-CN",
              try result.requireString("source", maximum: 64) == "pc-codex-cli",
              !(try result.requireString("text", maximum: 6000)).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw failure("BW_READER_DICTIONARY_RESPONSE", "词典响应与查询不匹配", retryable: false)
        }
        _ = try result.requireBool("cached")
        return reply
    }

    /// Opens the fixed WSS and completes the protocol-v3 HELLO exchange.
    /// Calling it while already ready/active is a no-op.
    func connect() async throws {
        switch state {
        case .ready, .starting, .active:
            return
        case .connecting, .authenticating, .stopping:
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_STATE",
                "电脑语音连接正在转换状态",
                retryable: true
            )
        case .disconnected, .failed:
            break
        }

        await closeTransport(finalState: nil)
        intentionalClose = false
        setState(.connecting)

        var upgradeRequest = URLRequest(url: configuration.endpoint)
        upgradeRequest.setValue(
            configuration.origin,
            forHTTPHeaderField: "Origin"
        )
        upgradeRequest.setValue(
            "no-store",
            forHTTPHeaderField: "Cache-Control"
        )
        upgradeRequest.timeoutInterval = TimeInterval(
            DirectVoiceProtocol.openTimeoutNanoseconds
        ) / 1_000_000_000

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.timeoutIntervalForRequest =
            upgradeRequest.timeoutInterval
        sessionConfiguration.timeoutIntervalForResource =
            TimeInterval(
                DirectVoiceProtocol.startTimeoutNanoseconds
            ) / 1_000_000_000
        sessionConfiguration.waitsForConnectivity = false

        let session = URLSession(configuration: sessionConfiguration)
        let socket = session.webSocketTask(with: upgradeRequest)
        socket.maximumMessageSize = DirectVoiceProtocol.maximumMessageBytes
        urlSession = session
        webSocket = socket
        socket.resume()

        receiveTask = Task { [weak self, weak socket] in
            guard let self, let socket else { return }
            await self.receiveLoop(socket)
        }

        setState(.authenticating)
        do {
            let hello = try await request(
                action: "hello",
                fields: [
                    "protocolVersion": .number(
                        Double(DirectVoiceProtocol.protocolVersion)
                    ),
                ],
                timeoutNanoseconds:
                    DirectVoiceProtocol.openTimeoutNanoseconds
            )
            try validateHello(hello)
            setState(.ready)
        } catch {
            let received = normalize(
                error,
                code: "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
                message: "Windows 桥接器离线或 WSS 连接失败",
                retryable: true
            )
            // A previous PWA snapshot socket may need a brief moment to release
            // the Windows single-client slot.  Mark only transport/timeout
            // handshake failures retryable; the bridge may retry once.  Schema
            // and contract failures remain terminal and are never looped.
            let retryableHandshakeCodes: Set<String> = [
                "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
                "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
                "BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
            ]
            let normalized = retryableHandshakeCodes.contains(received.code)
                ? DirectVoiceFailure(
                    code: received.code,
                    message: received.message,
                    retryable: true
                )
                : received
            await failConnection(normalized)
            throw normalized
        }
    }

    /// Starts one exact Windows voice session.  This convenience method opens
    /// and authenticates the WSS first when necessary.
    func start(
        appKind: DirectVoiceTargetApp = .codexDesktop,
        takeover: Bool = false
    ) async throws -> DirectVoiceSession {
        guard configuration != .readerContext else { throw failure("BW_READER_CONTEXT_AUDIO","数据连接不能启动语音",retryable:false) }
        if state == .disconnected || state == .failed {
            try await connect()
        }
        guard state == .ready, activeSession == nil else {
            throw failure(
                "BW_COMPUTER_VOICE_ALREADY_ACTIVE",
                "电脑客户端通话已经启动或正在启动",
                retryable: false
            )
        }

        let generated = makeSession()
        let session = DirectVoiceSession(
            id: generated.id,
            startedAt: Date()
        )
        activeSession = session
        activeSessionBytes = generated.bytes
        heartbeatSequence = 0
        uplinkSequence = 0
        uplinkTimestampBase = currentEpochMicroseconds()
        uplinkSendTail = nil
        uplinkSendGeneration = 0
        downlinkNextSequence = 0
        downlinkLastTimestamp = nil
        setState(.starting)

        do {
            // Keep the original Codex wire shape exactly compatible with the
            // stable bridge.  Only the newer GPT Classic target needs an
            // explicit appKind discriminator.
            var startFields: [String: DirectJSONValue] = [
                "sessionId": .string(session.id),
            ]
            if appKind != .codexDesktop {
                startFields["appKind"] = .string(appKind.rawValue)
            }
            if takeover {
                startFields["takeover"] = .bool(true)
            }
            let payload = try await request(
                action: "start",
                fields: startFields,
                timeoutNanoseconds:
                    DirectVoiceProtocol.startTimeoutNanoseconds
            )
            try validateStart(payload, session: session)
            setState(.active)
            startHeartbeat()
            return session
        } catch {
            let normalized = normalize(
                error,
                code: "BW_COMPUTER_VOICE_DIRECT_START_FAILED",
                message: "Windows 桥接器启动失败",
                retryable: false
            )
            await failConnection(normalized)
            throw normalized
        }
    }

    /// Relays Reader context over the already-active native voice WSS.  The
    /// page never opens a second socket while native voice owns the session.
    func requestReaderContext(
        action: String,
        fields: [String: DirectJSONValue]
    ) async throws -> DirectJSONValue {
        guard action == "context" || action == "active-reading" else {
            throw failure(
                "BW_NATIVE_COMPUTER_CONTEXT_ACTION",
                "Reader 原生上下文动作不在白名单内",
                retryable: false
            )
        }
        guard state == .active,
              let session = activeSession,
              fields["sessionId"] == .string(session.id) else {
            throw failure(
                "BW_NATIVE_COMPUTER_CONTEXT_SESSION",
                "Reader 原生上下文不属于当前语音会话",
                retryable: true
            )
        }
        return try await request(
            action: action,
            fields: fields,
            timeoutNanoseconds: DirectVoiceProtocol.requestTimeoutNanoseconds
        )
    }

    /// Sends exactly one 20 ms / 48 kHz / mono / s16le microphone payload.
    /// The caller supplies only 1,920 payload bytes; session, track, sequence
    /// and timestamp are generated and bound here.
    func sendUplinkPCM(_ pcm: Data) async throws {
        guard state == .active,
              let socket = webSocket,
              let sessionBytes = activeSessionBytes,
              pcm.count == DirectVoiceProtocol.pcmPayloadBytes else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME",
                "Reader 麦克风 PCM 帧或连接状态无效",
                retryable: state != .active
            )
        }
        guard uplinkSequence < UInt32.max else {
            let exhausted = failure(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_SEQUENCE_INVALID",
                "Reader 麦克风 PCM 序号已耗尽",
                retryable: false
            )
            await failConnection(exhausted)
            throw exhausted
        }

        let sequence = uplinkSequence
        let timestamp = uplinkTimestampBase.addingReportingOverflow(
            UInt64(sequence)
                * DirectVoiceProtocol.pcmFrameDurationMicroseconds
        )
        guard !timestamp.overflow else {
            let overflow = failure(
                "BW_COMPUTER_VOICE_DIRECT_UPLINK_TIMESTAMP_INVALID",
                "Reader 麦克风 PCM 时间戳溢出",
                retryable: false
            )
            await failConnection(overflow)
            throw overflow
        }
        let frame = encodePCMFrame(
            track: .browserMicrophone,
            sessionBytes: sessionBytes,
            sequence: sequence,
            timestampMicroseconds: timestamp.partialValue,
            payload: pcm
        )

        // Reserve the sequence before suspension.  Concurrent audio callbacks
        // therefore cannot reuse a sequence; any send failure closes the exact
        // session, so a skipped sequence is never retried on this connection.
        uplinkSequence += 1
        let previousSend = uplinkSendTail
        uplinkSendGeneration += 1
        let sendGeneration = uplinkSendGeneration
        let sendTask = Task<Void, Error> {
            if let previousSend {
                try await previousSend.value
            }
            try await socket.send(.data(frame))
        }
        uplinkSendTail = sendTask
        do {
            try await sendTask.value
            if uplinkSendGeneration == sendGeneration {
                uplinkSendTail = nil
            }
        } catch {
            let normalized = normalize(
                error,
                code:
                    "BW_COMPUTER_VOICE_DIRECT_UPLINK_DISCONNECTED",
                message: "Reader 麦克风 PCM 发送失败",
                retryable: true
            )
            await failConnection(normalized)
            throw normalized
        }
    }

    /// A side-effect-free STATUS request useful before presenting the call UI.
    func status() async throws -> DirectVoiceRuntimeStatus {
        try parseStatusResult(await statusDetails())
    }

    /// Read-only settings detail. Uses STATUS only; never requests audio,
    /// microphone permission, START or desktop application activation.
    func statusDetails() async throws -> DirectJSONValue {
        if state == .disconnected || state == .failed {
            try await connect()
        }
        let payload = try await request(
            action: "status",
            fields: [:],
            timeoutNanoseconds:
                DirectVoiceProtocol.requestTimeoutNanoseconds
        )
        _ = try parseStatusResult(payload)
        return payload
    }

    /// 把用户**打字说的话**送进正在进行的那通 Codex 语音。
    ///
    /// 用户 2026-09-11 提的「把电脑语音模式下那个输入框利用起来」。
    ///
    /// ⚠ App 上这条**必须走原生**：网页侧 `RC.computerVoice` 那套 DirectSocket
    /// 在 App 里根本没有 session（状态是原生推进去的），所以 JS 直接发会
    /// 静静失败 —— 实测就是"输入框绿了、回答却来自文字助手"。
    ///
    /// 桥那侧不在通话时回 `{ok:false, reason:"not-in-call"}`，不是异常。
    func codexType(text: String, attachmentIDs: [String] = [], submissionID: String? = nil) async throws -> Bool {
        var fields: [String: DirectJSONValue] = ["text": .string(text)]
        if !attachmentIDs.isEmpty {
            guard let submissionID else { return false }
            fields["attachmentIds"] = .array(attachmentIDs.map { .string($0) })
            fields["submissionId"] = .string(submissionID)
        }
        let payload = try await request(
            action: "codex-type",
            fields: fields,
            timeoutNanoseconds:
                DirectVoiceProtocol.requestTimeoutNanoseconds
        )
        return payload.objectValue?["ok"]?.boolValue ?? false
    }

    /// Gracefully stops an active session, then always releases the WSS.
    func stop(reason: String = "client-stop") async throws {
        heartbeatTask?.cancel()
        heartbeatTask = nil
        uplinkSendTail?.cancel()
        uplinkSendTail = nil

        guard let session = activeSession else {
            await closeTransport(finalState: .disconnected, reason: reason)
            return
        }

        // A START whose outcome is not yet known cannot safely be followed by
        // STOP on the same serial server connection. Closing lets the server's
        // owner lease cancel it fail-closed.
        guard state == .active else {
            await closeTransport(finalState: .disconnected, reason: reason)
            return
        }

        setState(.stopping)
        do {
            let payload = try await request(
                action: "stop",
                fields: ["sessionId": .string(session.id)],
                timeoutNanoseconds:
                    DirectVoiceProtocol.requestTimeoutNanoseconds
            )
            try validateStop(payload, session: session)
            await closeTransport(finalState: .disconnected, reason: reason)
        } catch {
            let normalized = normalize(
                error,
                code: "BW_COMPUTER_VOICE_DIRECT_STOP_FAILED",
                message: "Windows 桥接器停止失败",
                retryable: true
            )
            eventHandler(.error(normalized))
            await closeTransport(finalState: .disconnected, reason: reason)
            throw normalized
        }
    }

    /// Immediately releases the transport. Active Windows resources still
    /// fail closed through the server's connection-owner lease.
    func disconnect() async {
        await closeTransport(finalState: .disconnected)
    }

    // MARK: - Requests

    private func request(
        action: String,
        fields: [String: DirectJSONValue],
        timeoutNanoseconds: UInt64
    ) async throws -> DirectJSONValue {
        guard directVoiceSafeID(action),
              let socket = webSocket,
              pending.count < DirectVoiceProtocol.maximumPendingRequests else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_CAPACITY",
                "Windows 桥接器连接不可用或待处理请求过多",
                retryable: webSocket == nil
            )
        }

        let requestID = makeRequestID()
        var envelope = fields
        envelope["contract"] = .string(DirectVoiceProtocol.contract)
        envelope["type"] = .string(action)
        envelope["requestId"] = .string(requestID)

        let encoded: Data
        do {
            encoded = try encoder.encode(DirectJSONValue.object(envelope))
        } catch {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "Windows 桥接请求不能序列化",
                retryable: false
            )
        }
        guard encoded.count <= DirectVoiceProtocol.maximumMessageBytes,
              let text = String(data: encoded, encoding: .utf8) else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_CAPACITY",
                "Windows 桥接请求超过 64 KiB",
                retryable: false
            )
        }

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<DirectJSONValue, Error>) in
                let timeoutTask = Task { [weak self] in
                    do {
                        try await Task.sleep(
                            nanoseconds: timeoutNanoseconds
                        )
                    } catch {
                        return
                    }
                    await self?.requestTimedOut(
                        requestID,
                        action: action
                    )
                }
                pending[requestID] = PendingRequest(
                    action: action,
                    continuation: continuation,
                    timeoutTask: timeoutTask
                )
                Task { [weak self, weak socket] in
                    guard let self, let socket else { return }
                    do {
                        try await socket.send(.string(text))
                    } catch {
                        await self.requestSendFailed(
                            requestID,
                            error: error
                        )
                    }
                }
            }
        } onCancel: {
            Task { [weak self] in
                await self?.cancelRequest(requestID)
            }
        }
    }

    private func requestTimedOut(_ requestID: String, action: String) async {
        guard let entry = pending.removeValue(forKey: requestID) else {
            return
        }
        let timedOut = failure(
            action == "start"
                ? "BW_COMPUTER_VOICE_DIRECT_START_UNKNOWN"
                : "BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
            action == "start"
                ? "Windows 启动结果未知；连接已关闭，不会自动重试"
                : "Windows 桥接器请求超时",
            // A heartbeat is read-only and its timeout proves only that the
            // current transport is unhealthy, so rebuilding the session is
            // safe. Other requests may carry mutations whose outcome is
            // unknown after a timeout and must remain fail-closed.
            retryable: action == "heartbeat"
        )
        entry.continuation.resume(throwing: timedOut)
        await failConnection(timedOut)
    }

    private func requestSendFailed(
        _ requestID: String,
        error: Error
    ) async {
        guard let entry = pending.removeValue(forKey: requestID) else {
            return
        }
        entry.timeoutTask.cancel()
        let normalized = normalize(
            error,
            code: "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
            message: "Windows 桥接请求发送失败",
            retryable: true
        )
        entry.continuation.resume(throwing: normalized)
        await failConnection(normalized)
    }

    private func cancelRequest(_ requestID: String) {
        guard let entry = pending.removeValue(forKey: requestID) else {
            return
        }
        entry.timeoutTask.cancel()
        entry.continuation.resume(throwing: CancellationError())
    }

    // MARK: - Receive

    private func receiveLoop(_ socket: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            do {
                let message = try await socket.receive()
                guard socket === webSocket else { return }
                switch message {
                case .string(let text):
                    try handleText(text)
                case .data(let data):
                    try handleBinary(data)
                @unknown default:
                    throw failure(
                        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                        "Windows 桥接器发送了未知消息类型",
                        retryable: false
                    )
                }
            } catch is CancellationError {
                return
            } catch {
                guard !intentionalClose, socket === webSocket else {
                    return
                }
                let normalized = normalize(
                    error,
                    code: "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
                    message: "Windows 桥接器连接已断开",
                    retryable: true
                )
                await failConnection(normalized)
                return
            }
        }
    }

    private func handleText(_ text: String) throws {
        guard let data = text.data(using: .utf8),
              data.count <= DirectVoiceProtocol.maximumMessageBytes else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_CAPACITY",
                "Windows 桥接器控制帧超过 64 KiB",
                retryable: false
            )
        }
        let value: DirectJSONValue
        do {
            value = try decoder.decode(DirectJSONValue.self, from: data)
        } catch {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "Windows 桥接器响应无法解析",
                retryable: false
            )
        }
        guard let envelope = value.objectValue else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "Windows 桥接器响应必须是对象",
                retryable: false
            )
        }
        guard try envelope.requireString("contract", maximum: 128)
                == DirectVoiceProtocol.contract else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
                "Windows 桥接器合同版本不匹配",
                retryable: false
            )
        }
        let type = try envelope.requireString("type", maximum: 32)
        if type == "event" {
            try handleEvent(envelope)
        } else if type == "result" {
            try handleResult(envelope)
        } else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "Windows 桥接器消息类型无效",
                retryable: false
            )
        }
    }

    private func handleEvent(
        _ envelope: [String: DirectJSONValue]
    ) throws {
        try envelope.requireExactKeys([
            "contract", "type", "event", "payload",
        ])
        let name = try envelope.requireString("event", maximum: 32)
        // 迁出 3b：数据连接不登记阅读器来源，按理收不到这些事件；万一收到，出声并忽略，
        // 不因此断掉快照会话（桥会把它们留在队列里，等网页的快照链接回来处理）。
        if configuration == .readerContext,
           ["reader-result", "reader-visual-request", "reader-query-request", "reader-realtime-output"].contains(name) {
            if let readerEventHandler, let payload = envelope["payload"] {
                readerEventHandler(name, payload)
                return
            }
            eventHandler(.transientRetry(failure("BW_READER_CONTEXT_EVENT_UNHANDLED",
                "数据连接收到未接管的阅读器事件 \(name)，已忽略", retryable: true), attempt: 0))
            return
        }
        guard name == "status" else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "Windows 桥接器事件类型不受支持",
                retryable: false
            )
        }
        let payload = try envelope.requireObject("payload")
        try payload.requireExactKeys(["state", "reason"])
        let runtime = DirectVoiceRuntimeStatus(
            state: try payload.requireString("state", maximum: 64),
            reason: try optionalString(payload["reason"], maximum: 512),
            ready: nil,
            localOptIn: nil,
            hostReady: nil,
            captureActive: nil
        )
        eventHandler(.runtime(runtime))
    }

    private func handleResult(
        _ envelope: [String: DirectJSONValue]
    ) throws {
        try envelope.requireExactKeys(
            ["contract", "type", "requestId", "ok", "action"],
            optional: ["payload", "error"]
        )
        let requestID = try envelope.requireString(
            "requestId",
            maximum: 160
        )
        let action = try envelope.requireString("action", maximum: 32)
        guard directVoiceSafeID(requestID),
              let entry = pending.removeValue(forKey: requestID),
              entry.action == action else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_REQUEST",
                "Windows 桥接器返回未知或错配的 requestId",
                retryable: false
            )
        }
        entry.timeoutTask.cancel()

        let ok = try envelope.requireBool("ok")
        if ok {
            guard let payload = envelope["payload"],
                  envelope["error"] == nil else {
                entry.continuation.resume(
                    throwing: failure(
                        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                        "Windows 桥接器成功响应字段无效",
                        retryable: false
                    )
                )
                throw failure(
                    "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                    "Windows 桥接器成功响应字段无效",
                    retryable: false
                )
            }
            entry.continuation.resume(returning: payload)
            return
        }

        guard envelope["payload"] == nil,
              let remote = envelope["error"]?.objectValue else {
            let invalid = failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "Windows 桥接器失败响应字段无效",
                retryable: false
            )
            entry.continuation.resume(throwing: invalid)
            throw invalid
        }
        do {
            try remote.requireExactKeys(["code", "message", "retryable"])
            let remoteFailure = DirectVoiceFailure(
                code: try remote.requireString("code", maximum: 160),
                message: try remote.requireString(
                    "message",
                    maximum: 1_024
                ),
                retryable: try remote.requireBool("retryable")
            )
            entry.continuation.resume(throwing: remoteFailure)
        } catch {
            let invalid = normalize(
                error,
                code: "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                message: "Windows 桥接器错误响应无效",
                retryable: false
            )
            entry.continuation.resume(throwing: invalid)
            throw invalid
        }
    }

    private func handleBinary(_ data: Data) throws {
        guard state == .starting || state == .active,
              let sessionBytes = activeSessionBytes,
              data.count == DirectVoiceProtocol.pcmFrameBytes else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_UNEXPECTED",
                "Windows 在非通话连接发送了 PCM",
                retryable: false
            )
        }
        let bytes = [UInt8](data)
        guard bytes[0] == 0x42,
              bytes[1] == 0x57,
              bytes[2] == 0x43,
              bytes[3] == 0x56,
              bytes[4] == 1 else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_MAGIC",
                "Windows PCM magic 或版本无效",
                retryable: false
            )
        }
        guard bytes[5] == DirectVoiceTrack.appOutput.rawValue,
              readUInt16LE(bytes, 6) == 0 else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_TRACK",
                "Windows PCM track 或 flags 无效",
                retryable: false
            )
        }
        guard Data(bytes[8..<24]) == sessionBytes else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_SESSION",
                "Windows PCM session 与当前通话不匹配",
                retryable: false
            )
        }

        let sequence = readUInt32LE(bytes, 24)
        guard sequence == downlinkNextSequence else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE",
                "Windows PCM sequence 不连续",
                retryable: false
            )
        }
        let timestamp = readUInt64LE(bytes, 28)
        if let previous = downlinkLastTimestamp, timestamp <= previous {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_TIMESTAMP",
                "Windows PCM timestamp 未严格递增",
                retryable: false
            )
        }
        guard downlinkNextSequence < UInt32.max else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE",
                "Windows PCM sequence 已耗尽",
                retryable: false
            )
        }
        downlinkNextSequence += 1
        downlinkLastTimestamp = timestamp

        let payloadRange =
            DirectVoiceProtocol.pcmHeaderBytes..<DirectVoiceProtocol.pcmFrameBytes
        let payload = Data(bytes[payloadRange])
        eventHandler(.downlinkPCM(DirectVoicePCMFrame(
            track: .appOutput,
            sequence: sequence,
            timestampMicroseconds: timestamp,
            payload: payload
        )))
    }

    // MARK: - Heartbeat

    /// 心跳连续失败多少次才判定连接已死。
    ///
    /// ⚠ 这里原来是**一次失败就整条拆掉**，而单次请求超时是 7 秒
    /// （DirectVoiceProtocol.requestTimeoutNanoseconds）—— 也就是说客户端比
    /// **服务端还严**：桥的合同允许 15 秒收不到心跳才断
    /// （DirectBridgeContract.ClientHeartbeatTimeoutMilliseconds = 15_000）。
    /// 一次 >7s 的 RTT 抖动（iOS 切后台调度、Tailscale 路径切换、Wi-Fi→蜂窝）
    /// 就足以把整通电话拆掉，而服务端此刻还愿意再等 8 秒。
    /// 这正是用户 2026-08-23 说的"稍微有一点网络波动就断开"。
    ///
    /// 改成连续 2 次：坏连接最多晚 ~5 秒（一个心跳间隔）被发现，
    /// 换掉一整类由瞬时抖动造成的误杀。服务端合同不用动。
    private static let heartbeatFailuresBeforeGivingUp = 2

    private func startHeartbeat() {
        heartbeatTask?.cancel()
        heartbeatTask = Task { [weak self] in
            var consecutiveFailures = 0
            while !Task.isCancelled {
                do {
                    try await Task.sleep(
                        nanoseconds: UInt64(
                            DirectVoiceProtocol
                                .heartbeatIntervalMilliseconds
                        ) * 1_000_000
                    )
                    guard !Task.isCancelled else { return }
                    try await self?.sendHeartbeat()
                    consecutiveFailures = 0
                } catch is CancellationError {
                    return
                } catch {
                    guard let self else { return }
                    consecutiveFailures += 1
                    if consecutiveFailures
                        < Self.heartbeatFailuresBeforeGivingUp {
                        // 还没到判死线：留一声，别静默重试 ——
                        // 否则"抖了一下"和"真断了"在日志里长得一样。
                        await self.noteHeartbeatRetry(
                            error,
                            attempt: consecutiveFailures
                        )
                        continue
                    }
                    let normalized = await self.normalize(
                        error,
                        code:
                            "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
                        message: "Windows 桥接器心跳连续失败",
                        retryable: true
                    )
                    await self.failConnection(normalized)
                    return
                }
            }
        }
    }

    private func sendHeartbeat() async throws {
        guard state == .active,
              let session = activeSession,
              heartbeatSequence < UInt32.max else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_SEQUENCE_INVALID",
                "电脑语音心跳状态或序号无效",
                retryable: false
            )
        }
        heartbeatSequence += 1
        let sequence = heartbeatSequence
        let payload = try await request(
            action: "heartbeat",
            fields: [
                "sessionId": .string(session.id),
                "sequence": .number(Double(sequence)),
            ],
            timeoutNanoseconds:
                DirectVoiceProtocol.requestTimeoutNanoseconds
        )
        let object = try requireObject(payload, label: "HEARTBEAT")
        try object.requireExactKeys(["sessionId", "sequence", "state"])
        guard try object.requireString("sessionId", maximum: 160)
                == session.id,
              try object.requireUInt32("sequence") == sequence,
              try object.requireString("state", maximum: 32)
                == "active" else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
                "Windows 桥接器 HEARTBEAT 回执无效",
                retryable: false
            )
        }
    }

    // MARK: - Payload validation

    private func validateHello(_ value: DirectJSONValue) throws {
        let object = try requireObject(value, label: "HELLO")
        try object.requireExactKeys(["protocolVersion", "limits"])
        guard try object.requireInt("protocolVersion")
                == DirectVoiceProtocol.protocolVersion else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
                "Windows 桥接器直连协议版本不匹配",
                retryable: false
            )
        }
        let limits = try object.requireObject("limits")
        try limits.requireExactKeys([
            "maxMessageBytes",
            "pcmFrameBytes",
            "pcmQueueLimitMs",
            "uplinkTrack",
            "uplinkQueueLimitMs",
            "heartbeatIntervalMs",
            "heartbeatTimeoutMs",
        ])
        guard try limits.requireInt("maxMessageBytes")
                == DirectVoiceProtocol.maximumMessageBytes,
              try limits.requireInt("pcmFrameBytes")
                == DirectVoiceProtocol.pcmFrameBytes,
              try limits.requireInt("pcmQueueLimitMs")
                == DirectVoiceProtocol.pcmQueueLimitMilliseconds,
              try limits.requireInt("uplinkTrack")
                == Int(DirectVoiceTrack.browserMicrophone.rawValue),
              try limits.requireInt("uplinkQueueLimitMs")
                == DirectVoiceProtocol.uplinkQueueLimitMilliseconds,
              try limits.requireInt("heartbeatIntervalMs")
                == DirectVoiceProtocol.heartbeatIntervalMilliseconds,
              try limits.requireInt("heartbeatTimeoutMs")
                == DirectVoiceProtocol.heartbeatTimeoutMilliseconds else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
                "Windows 桥接器容量合同不匹配",
                retryable: false
            )
        }
    }

    private func validateStart(
        _ value: DirectJSONValue,
        session: DirectVoiceSession
    ) throws {
        let object = try requireObject(value, label: "START")
        try object.requireExactKeys(["sessionId", "state", "media"])
        let media = try object.requireObject("media")
        try media.requireExactKeys(["hostReady", "captureActive"])
        guard try object.requireString("sessionId", maximum: 160)
                == session.id,
              try object.requireString("state", maximum: 32)
                == "active",
              try media.requireBool("hostReady"),
              try media.requireBool("captureActive") else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_START",
                "Windows 桥接器未确认本次 START",
                retryable: false
            )
        }
    }

    private func validateStop(
        _ value: DirectJSONValue,
        session: DirectVoiceSession
    ) throws {
        let object = try requireObject(value, label: "STOP")
        try object.requireExactKeys(["sessionId", "state"])
        guard try object.requireString("sessionId", maximum: 160)
                == session.id,
              try object.requireString("state", maximum: 32)
                == "idle" else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_STOP",
                "Windows 桥接器 STOP 回执无效",
                retryable: false
            )
        }
    }

    private func parseStatusResult(
        _ value: DirectJSONValue
    ) throws -> DirectVoiceRuntimeStatus {
        let object = try requireObject(value, label: "STATUS")
        try object.requireExactKeys([
            "ready",
            "state",
            "reason",
            "localOptIn",
            "lastError",
            "media",
        ], optional:["codexVoice"])
        let media = try object.requireObject("media")
        try media.requireExactKeys(["hostReady", "captureActive"])
        if let raw = object["codexVoice"] {
            let voice = try requireObject(raw,label:"STATUS voice")
            try voice.requireExactKeys(["status","active","source","shortcutSent"],optional:["keepActive","ladder","push"])
            let status = try voice.requireString("status",maximum:32)
            guard ["available","unavailable","error"].contains(status),
                  (status == "available" ? voice["active"]?.boolValue != nil : voice["active"] == .null),
                  voice["source"] == .null || voice["source"]?.stringValue != nil else {
                throw failure("BW_COMPUTER_VOICE_DIRECT_SCHEMA","STATUS voice 字段无效",retryable:false)
            }
            _ = try voice.requireBool("shortcutSent")
        }
        if let lastError = object["lastError"] {
            guard lastError == .null || lastError.objectValue != nil else {
                throw failure(
                    "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                    "STATUS lastError 字段无效",
                    retryable: false
                )
            }
        }
        return DirectVoiceRuntimeStatus(
            state: try object.requireString("state", maximum: 64),
            reason: try optionalString(object["reason"], maximum: 512),
            ready: try object.requireBool("ready"),
            localOptIn: try object.requireBool("localOptIn"),
            hostReady: try media.requireBool("hostReady"),
            captureActive: try media.requireBool("captureActive")
        )
    }

    // MARK: - Lifecycle helpers

    /// 心跳瞬时失败但还没到判死线时的留痕。
    ///
    /// ⚠ 不能走 `.error` —— 那一档在上层被当作"通话结束"处理
    /// （NativeVoiceBridge.handleSessionFailure），报一次就把会话收了，
    /// 等于把"容忍"变成了"照样断"。
    private func noteHeartbeatRetry(
        _ error: Error,
        attempt: Int
    ) async {
        let normalized = await normalize(
            error,
            code: "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT_RETRY",
            message: "Windows 桥接器心跳超时，正在重试",
            retryable: true
        )
        eventHandler(.transientRetry(normalized, attempt: attempt))
    }

    private func failConnection(_ error: DirectVoiceFailure) async {
        guard state != .failed || webSocket != nil else { return }
        // 2026-09-15：桥主动结束通话时会在 close reason 里带上原因码（语音核心结束了这通）。
        // 上行发送往往比 status 帧先失败，这里统一把"带这个原因的断开"改写成不可续接的挂断，
        // 否则会被当成掉线、1 秒内自动重拨（实录 10:19 两次）。
        var resolved = error
        if let reasonData = webSocket?.closeReason,
           let reasonText = String(data: reasonData, encoding: .utf8),
           reasonText.contains("VOICE_ENDED_BY_CORE") {
            resolved = DirectVoiceFailure(
                code: "BW_COMPUTER_VOICE_DIRECT_VOICE_ENDED_BY_CORE",
                message: "语音核心已结束通话",
                retryable: false
            )
        }
        setState(.failed)
        eventHandler(.error(resolved))
        await closeTransport(finalState: nil, closeCode: .protocolError)
    }

    private func closeTransport(
        finalState: DirectVoiceState?,
        reason: String = "client-stop",
        closeCode: URLSessionWebSocketTask.CloseCode = .normalClosure
    ) async {
        intentionalClose = true
        heartbeatTask?.cancel()
        heartbeatTask = nil
        uplinkSendTail?.cancel()
        uplinkSendTail = nil
        receiveTask?.cancel()
        receiveTask = nil

        let cancelled = failure(
            "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
            "Windows 桥接连接已关闭",
            retryable: false
        )
        let waiting = pending
        pending.removeAll()
        for (_, entry) in waiting {
            entry.timeoutTask.cancel()
            entry.continuation.resume(throwing: cancelled)
        }

        let boundedReason = Data(reason.utf8.prefix(120))
        webSocket?.cancel(with: closeCode, reason: boundedReason)
        webSocket = nil
        urlSession?.invalidateAndCancel()
        urlSession = nil
        activeSession = nil
        activeSessionBytes = nil
        contextSessionID = nil
        heartbeatSequence = 0
        uplinkSequence = 0
        uplinkTimestampBase = 0
        uplinkSendGeneration = 0
        downlinkNextSequence = 0
        downlinkLastTimestamp = nil

        if let finalState {
            setState(finalState)
        }
    }

    private func setState(_ newState: DirectVoiceState) {
        guard state != newState else { return }
        state = newState
        eventHandler(.state(newState))
    }

    private func failure(
        _ code: String,
        _ message: String,
        retryable: Bool
    ) -> DirectVoiceFailure {
        DirectVoiceFailure(
            code: code,
            message: message,
            retryable: retryable
        )
    }

    private func normalize(
        _ error: Error,
        code: String,
        message: String,
        retryable: Bool
    ) -> DirectVoiceFailure {
        if let direct = error as? DirectVoiceFailure {
            return direct
        }
        if error is CancellationError {
            return failure(
                "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
                "Windows 桥接操作已取消",
                retryable: false
            )
        }
        let detail = error.localizedDescription
        return failure(
            code,
            detail.isEmpty ? message : "\(message)：\(detail)",
            retryable: retryable
        )
    }

    private func requireObject(
        _ value: DirectJSONValue,
        label: String
    ) throws -> [String: DirectJSONValue] {
        guard let object = value.objectValue else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "\(label) 响应必须是对象",
                retryable: false
            )
        }
        return object
    }

    private func optionalString(
        _ value: DirectJSONValue?,
        maximum: Int
    ) throws -> String? {
        guard let value else { return nil }
        if value == .null { return nil }
        guard let string = value.stringValue,
              !string.isEmpty,
              string.count <= maximum else {
            throw failure(
                "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
                "可选字符串字段无效",
                retryable: false
            )
        }
        return string
    }

    // MARK: - Wire encoding

    private func makeRequestID() -> String {
        "request-\(base64URL(randomBytes(count: 16)))-\(UUID().uuidString)"
    }

    private func makeSession() -> (id: String, bytes: Data) {
        let bytes = randomBytes(count: 16)
        return ("session-\(base64URL(bytes))", bytes)
    }

    private func randomBytes(count: Int) -> Data {
        var generator = SystemRandomNumberGenerator()
        let bytes = (0..<count).map { _ in
            UInt8.random(in: UInt8.min...UInt8.max, using: &generator)
        }
        return Data(bytes)
    }

    private func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private func currentEpochMicroseconds() -> UInt64 {
        let seconds = max(0, Date().timeIntervalSince1970)
        return UInt64(seconds * 1_000_000)
    }

    private func encodePCMFrame(
        track: DirectVoiceTrack,
        sessionBytes: Data,
        sequence: UInt32,
        timestampMicroseconds: UInt64,
        payload: Data
    ) -> Data {
        precondition(sessionBytes.count == 16)
        precondition(payload.count == DirectVoiceProtocol.pcmPayloadBytes)
        var bytes = [UInt8](
            repeating: 0,
            count: DirectVoiceProtocol.pcmFrameBytes
        )
        bytes[0] = 0x42
        bytes[1] = 0x57
        bytes[2] = 0x43
        bytes[3] = 0x56
        bytes[4] = 1
        bytes[5] = track.rawValue
        writeUInt16LE(0, into: &bytes, at: 6)
        bytes.replaceSubrange(8..<24, with: sessionBytes)
        writeUInt32LE(sequence, into: &bytes, at: 24)
        writeUInt64LE(timestampMicroseconds, into: &bytes, at: 28)
        let payloadRange =
            DirectVoiceProtocol.pcmHeaderBytes..<DirectVoiceProtocol.pcmFrameBytes
        bytes.replaceSubrange(payloadRange, with: payload)
        return Data(bytes)
    }

    private func readUInt16LE(_ bytes: [UInt8], _ offset: Int) -> UInt16 {
        UInt16(bytes[offset])
            | (UInt16(bytes[offset + 1]) << 8)
    }

    private func readUInt32LE(_ bytes: [UInt8], _ offset: Int) -> UInt32 {
        UInt32(bytes[offset])
            | (UInt32(bytes[offset + 1]) << 8)
            | (UInt32(bytes[offset + 2]) << 16)
            | (UInt32(bytes[offset + 3]) << 24)
    }

    private func readUInt64LE(_ bytes: [UInt8], _ offset: Int) -> UInt64 {
        var value: UInt64 = 0
        for index in 0..<8 {
            value |= UInt64(bytes[offset + index]) << UInt64(index * 8)
        }
        return value
    }

    private func writeUInt16LE(
        _ value: UInt16,
        into bytes: inout [UInt8],
        at offset: Int
    ) {
        for index in 0..<2 {
            bytes[offset + index] = UInt8(
                truncatingIfNeeded: value >> UInt16(index * 8)
            )
        }
    }

    private func writeUInt32LE(
        _ value: UInt32,
        into bytes: inout [UInt8],
        at offset: Int
    ) {
        for index in 0..<4 {
            bytes[offset + index] = UInt8(
                truncatingIfNeeded: value >> UInt32(index * 8)
            )
        }
    }

    private func writeUInt64LE(
        _ value: UInt64,
        into bytes: inout [UInt8],
        at offset: Int
    ) {
        for index in 0..<8 {
            bytes[offset + index] = UInt8(
                truncatingIfNeeded: value >> UInt64(index * 8)
            )
        }
    }
}
