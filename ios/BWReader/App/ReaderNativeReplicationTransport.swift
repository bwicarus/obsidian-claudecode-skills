import Foundation

/// The existing context-only protocol, carried directly by URLSession. This
/// socket is separate from voice, so long card uploads cannot delay PCM.
actor ReaderNativeReplicationTransport {
    private let socket = DirectVoiceSocket(configuration:.readerContext) { _ in }

    func send(_ data: Data, mutationID: String) async throws -> String {
        _ = try await socket.openReaderContext()
        let requests = try Self.requests(data,mutationID:mutationID)
        var outcome = ""
        for (index,request) in requests.enumerated() {
            try Task.checkCancellation()
            let reply = try await socket.requestReaderData(action:request.action,fields:request.fields)
            outcome = try Self.outcome(reply,mutationID:mutationID,partial:index < requests.count - 1)
        }
        return outcome
    }

    func close() async { await socket.disconnect() }

    struct Request: Sendable { let action:String; let fields:[String:DirectJSONValue] }
    static func requests(_ data: Data, mutationID: String) throws -> [Request] {
        guard !data.isEmpty, data.count <= 6 * 1024 * 1024,
              mutationID.range(of:"^mut-v2-[a-f0-9]{32}$",options:.regularExpression) != nil else { throw invalid() }
        let value = try JSONDecoder().decode(DirectJSONValue.self,from:data)
        guard value.objectValue?["contract"] == .string("replication-command/1"),
              value.objectValue?["op"]?.objectValue?["mutationId"] == .string(mutationID) else { throw invalid() }
        // Include JSONEncoder's escaping in the frame budget, not just the
        // input bytes. Larger envelopes use the existing base64 chunk path.
        let encoded = try JSONEncoder().encode(value)
        if encoded.count <= 195 * 1024 { return [.init(action:"replication-command",fields:["envelope":value])] }
        let base64 = [UInt8](data.base64EncodedString().utf8), size = 160 * 1024
        let count = (base64.count + size - 1) / size
        guard count <= 64 else { throw invalid() }
        return (0..<count).map { index in
            let part = String(decoding:base64[(index * size)..<min(base64.count,(index + 1) * size)],as:UTF8.self)
            return Request(action:"replication-command-chunk",fields:["chunk":.object([
                "mutationId":.string(mutationID),"seq":.number(Double(index)),"total":.number(Double(count)),"part":.string(part)])])
        }
    }
    static func outcome(_ value: DirectJSONValue, mutationID:String, partial:Bool) throws -> String {
        guard let object = value.objectValue else { throw invalid() }
        try object.requireExactKeys(["contract","mutationId","outcome"],optional:["received"])
        guard object["contract"] == .string("replication-command/1"),object["mutationId"] == .string(mutationID),
              let result = object["outcome"]?.stringValue,
              partial ? result == "partial" : ["accepted","rejected"].contains(result) else { throw invalid() }
        return result
    }
    private static func invalid() -> DirectVoiceFailure {
        .init(code:"BW_NATIVE_REPLICATION_RESPONSE",message:"复制命令或回执不匹配，原命令已保留",retryable:false)
    }
}

/// One worker owns all book outboxes in this process. New writes only wake it;
/// they do not create a second transport or a second copy of the command.
@MainActor
final class ReaderNativeReplicationService {
    private let outbox: ReaderNativeReplicationOutbox
    private let report: (String) -> Void
    private let settled: () -> Void
    private var task: Task<Void,Never>?
    private var active = true

    init(store:ReaderNativeDataStore,report:@escaping (String)->Void,settled:@escaping ()->Void) {
        outbox = .init(store:store); self.report = report; self.settled = settled
    }
    func setActive(_ value:Bool) {
        active = value
        if value { wake() } else { task?.cancel() }
    }
    func wake() {
        guard active, task == nil else { return }
        task = Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                self.task = nil
                // Foreground can return while cancellation is unwinding.
                if self.active && Task.isCancelled { self.wake() }
            }
            var delay: UInt64 = 2
            while self.active && !Task.isCancelled {
                let transport = ReaderNativeReplicationTransport()
                do {
                    let pending = try self.outbox.pending()
                    if pending.isEmpty { self.settled(); break }
                    for entry in pending {
                        try Task.checkCancellation()
                        let outcome = try await transport.send(entry.envelope,mutationID:entry.mutationID)
                        if outcome == "accepted" {
                            try self.outbox.acknowledge(entry,mutationID:entry.mutationID,outcome:outcome,now:Int64(Date().timeIntervalSince1970 * 1000))
                        } else {
                            throw DirectVoiceFailure(code:"BW_NATIVE_REPLICATION_REJECTED",message:"服务器未接收复制命令，已保留本地队列",retryable:false)
                        }
                    }
                    await transport.close()
                    delay = 2
                } catch {
                    await transport.close()
                    if error is CancellationError || Task.isCancelled { break }
                    self.report(error.localizedDescription)
                    do { try await Task.sleep(nanoseconds:delay * 1_000_000_000) } catch { break }
                    delay = min(delay * 2,60)
                }
            }
        }
    }
}
