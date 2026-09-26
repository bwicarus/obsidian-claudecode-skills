import Foundation

/// 迁出 3b（2026-09-26）：后台通话期间由原生保持阅读器快照会话。
///
/// 背景：熄屏 / 切走时本机 runtime 停止、网页主动关快照链接（iOS 也会挂起后台 App 的
/// WebKit 进程），桥上的快照随之变 disabled —— 通话音频（原生）还在，但语音 AI 的
/// 「读当前页」等阅读器工具全部失效（用户报「软件语音时熄屏后语音链接会断开」）。
///
/// 做法：进后台且电脑语音仍在通话时，接过网页交出的最后一份 active-reading
/// （`reader-active-reading/1`，结构与网页快照链接发的完全相同，不在原生另造一份），
/// 用独立的 context 连接：
///   1. 重发当前页正文 —— 本机发送队列（native-outgoing-journal）里最新一条 page.context；
///   2. 发 active-reading，之后每 50 秒续一次（桥的心跳窗口是 60 秒）。
/// 回前台先关掉，网页照旧重建自己的快照链接。
///
/// 3b-2：沿用网页交出的来源编号登记 visual 来源，桥发来的事件由原生应答：
///   - 查询：读页 / 高亮 / 目录由 `answer` 回答（ReaderWebView 从 PDFKit 与本机数据库取），其余「unavailable」；
///   - 截图（笔迹视觉）：回「unavailable」；
///   - 实时输出 / 结果投递：回 rejected + `…UNAVAILABLE` —— 桥把持久输出（带 bind 的卡）留在队列，
///     等网页回来执行；非持久的（工具状态等）按原合同本就不进队列。
actor ReaderNativeBackgroundContext {
    struct QueryAnswer: Sendable {
        let status: String
        let result: DirectJSONValue
        let truncated: Bool
        static let unavailable = QueryAnswer(status: "unavailable", result: .object([:]), truncated: false)
    }
    typealias Answer = @Sendable (String, DirectJSONValue) async -> QueryAnswer

    private var socket: DirectVoiceSocket?
    private var heartbeat: Task<Void, Never>?
    private var generation = 0
    private let log: @Sendable (String) -> Void

    static let heartbeatNanoseconds: UInt64 = 50_000_000_000

    init(log: @escaping @Sendable (String) -> Void) { self.log = log }

    var isRunning: Bool { socket != nil }

    /// `active`：网页交出的 active 对象；`pageContext`：本机发送队列里最新一条 page.context 事件（可无）。
    /// `keepAlive`：每次续心跳前问一次「还在后台、还在通话吗」，否则交还（挂断后不再替网页续快照）。
    func start(active: DirectJSONValue, pageContext: DirectJSONValue?, sourceInstanceId: String?,
               answer: @escaping Answer, keepAlive: @escaping @Sendable () async -> Bool) async {
        await stop(reason: nil)
        generation += 1
        let ticket = generation
        let log = self.log
        let socket = DirectVoiceSocket(configuration: .readerContext) { event in
            switch event {
            case .transientRetry(let failure, _): log("[后台快照] \(failure.code) \(failure.message)")
            case .error(let failure): log("[后台快照] 连接出错 \(failure.code) \(failure.message)")
            default: break
            }
        }
        self.socket = socket
        do {
            _ = try await socket.openReaderContext()
            guard ticket == generation else { await socket.disconnect(); return }
            if let sourceInstanceId {
                await socket.setReaderEventHandler { [weak self] name, payload in
                    Task { await self?.handleReaderEvent(name, payload, answer: answer, ticket: ticket) }
                }
                let registration = try await socket.replyReader(action: "visual-register",
                    fields: ["sourceInstanceId": .string(sourceInstanceId)], includeSession: true)
                if registration.objectValue?["state"] != .string("registered") {
                    log("[后台快照] 来源登记回执异常，查询可能到不了原生")
                }
            } else {
                log("[后台快照] 网页没交出来源编号：只保持快照，不接查询")
            }
            if let pageContext {
                _ = try await socket.publishReaderContext(action: "context", fields: [
                    "contextContract": .string("reader-outgoing-context/1"), "event": pageContext])
                log("[后台快照] 已重发当前页正文")
            } else {
                log("[后台快照] 本机发送队列里没有当前页正文，只续阅读状态")
            }
            try await publishActive(active, ticket: ticket)
            heartbeat = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: Self.heartbeatNanoseconds)
                    guard !Task.isCancelled, let self else { return }
                    guard await keepAlive() else { await self.stop(reason: "通话已结束或已回前台"); return }
                    do { try await self.publishActive(active, ticket: ticket) }
                    catch { await self.failed(error, ticket: ticket); return }
                }
            }
            log("[后台快照] 已接管：通话中进入后台，由原生保持阅读器快照")
        } catch {
            await failed(error, ticket: ticket)
        }
    }

    /// 回前台 / 通话结束 / 换书时关掉。`reason` 为 nil 时不出声（内部重启）。
    func stop(reason: String?) async {
        generation += 1
        heartbeat?.cancel(); heartbeat = nil
        guard let socket else { return }
        self.socket = nil
        await socket.disconnect()
        if let reason { log("[后台快照] 已交还网页（\(reason)）") }
    }

    private func publishActive(_ active: DirectJSONValue, ticket: Int) async throws {
        guard ticket == generation, let socket, var object = active.objectValue else { return }
        object["observedAtEpochMs"] = .number((Date().timeIntervalSince1970 * 1000).rounded())
        _ = try await socket.publishReaderContext(action: "active-reading", fields: [
            "activeContract": .string("reader-active-reading/1"), "active": .object(object)])
    }

    private func failed(_ error: Error, ticket: Int) async {
        guard ticket == generation else { return }
        let detail = (error as? DirectVoiceFailure).map { $0.code + " " + $0.message } ?? error.localizedDescription
        log("[后台快照] 接管失败：\(detail)")
        await stop(reason: nil)
    }

    // MARK: 应答桥的阅读器事件

    private func handleReaderEvent(_ name: String, _ payload: DirectJSONValue, answer: Answer, ticket: Int) async {
        guard ticket == generation, let socket, let request = payload.objectValue else { return }
        func pass(_ keys: [String]) -> [String: DirectJSONValue] {
            var out: [String: DirectJSONValue] = [:]
            for key in keys { out[key] = request[key] ?? .null }
            return out
        }
        do {
            switch name {
            case "reader-query-request":
                let query = request["query"]?.stringValue ?? ""
                let reply = await answer(query, request["params"] ?? .object([:]))
                guard ticket == generation else { return }
                var fields = pass(["correlation", "sourceInstanceId", "snapshotRevision", "file", "query"])
                fields["status"] = .string(reply.status); fields["result"] = reply.result; fields["truncated"] = .bool(reply.truncated)
                _ = try await socket.replyReader(action: "reader-query", fields: fields, includeSession: true)
                log("[后台快照] 查询 \(query) → \(reply.status)")
            case "reader-visual-request":
                var fields = pass(["correlation", "sourceInstanceId", "snapshotRevision", "file", "page", "drawingRevision", "scope", "selectionId"])
                fields["status"] = .string("unavailable"); fields["mimeType"] = .string("")
                fields["chunkIndex"] = .number(0); fields["chunkCount"] = .number(0); fields["totalBytes"] = .number(0); fields["data"] = .string("")
                _ = try await socket.replyReader(action: "reader-visual", fields: fields, includeSession: true)
                log("[后台快照] 截图请求 → 不可用（后台）")
            case "reader-realtime-output":
                var fields = pass(["correlation", "sourceInstanceId"])
                fields["outcome"] = .string("rejected")
                fields["error"] = .string("BW_READER_REALTIME_OUTPUT_UNAVAILABLE: App 在后台（熄屏通话中），回到前台后执行")
                fields["bindOutcome"] = .null; fields["bindReason"] = .null; fields["sidebar"] = .null
                _ = try await socket.replyReader(action: "reader-realtime-output-ack", fields: fields, includeSession: true)
                let kind = request["kind"]?.stringValue ?? "?"
                log("[后台快照] 实时输出 \(kind) → 留在队列等回前台")
            case "reader-result":
                var fields = pass(["correlation"])
                fields["outcome"] = .string("rejected")
                fields["error"] = .string("BW_READER_RESULT_RECEIVER_UNAVAILABLE: App 在后台（熄屏通话中）")
                _ = try await socket.replyReader(action: "reader-result-ack", fields: fields, includeSession: false)
                log("[后台快照] 结果投递 → 不可用（后台）")
            default:
                return
            }
        } catch {
            let detail = (error as? DirectVoiceFailure).map { $0.code + " " + $0.message } ?? error.localizedDescription
            log("[后台快照] 应答 \(name) 失败：\(detail)")
        }
    }

    // MARK: 从本机存储取当前页正文

    /// native-outgoing-journal 记录里最新一条未被取代的 page.context（与网页上下文泵引导时的规则一致：
    /// 只从最近一条完整页上下文开始，被取代的那条正文已清空、不发）。
    static func latestPageContext(journalJSON: String) -> DirectJSONValue? {
        guard let envelope = try? JSONSerialization.jsonObject(with: Data(journalJSON.utf8)) as? [String: Any],
              let value = envelope["value"] as? [String: Any], let payload = value["payload"] as? [String: Any],
              let events = payload["events"] as? [[String: Any]] else { return nil }
        guard let event = events.last(where: { row in
            row["type"] as? String == "page.context" &&
                (row["page_context"] as? [String: Any])?["superseded"] as? Bool != true
        }) else { return nil }
        return jsonValue(event)
    }

    static func jsonValue(_ value: Any) -> DirectJSONValue? {
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value) else { return nil }
        return try? JSONDecoder().decode(DirectJSONValue.self, from: data)
    }
}
