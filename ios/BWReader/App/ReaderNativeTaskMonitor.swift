import Foundation

/// Read-only task tracking. Fetch retries never repeat submission or a client
/// effect. An uncertain consumer acknowledgement is terminal for this watcher.
actor ReaderNativeTaskMonitor {
    enum Kind: String, Sendable { case cli, write }
    enum Outcome: String, Sendable { case done, missing, timeout }
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    typealias Fetch = @Sendable () async throws -> Data
    typealias Deliver = @Sendable (Data) async throws -> Void
    private let kind: Kind
    private let fetch: Fetch
    private let deliver: Deliver
    private let delay: @Sendable (UInt64) async throws -> Void
    private let maximumChecks: Int
    private var started = false

    init(kind: Kind, maximumChecks: Int? = nil, fetch: @escaping Fetch, deliver: @escaping Deliver,
         delay: @escaping @Sendable (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0 * 1_000_000) }) {
        self.kind = kind; self.fetch = fetch; self.deliver = deliver; self.delay = delay
        self.maximumChecks = maximumChecks ?? (kind == .cli ? 601 : 121)
    }

    static func path(taskID: String) throws -> String {
        guard !taskID.isEmpty, taskID.utf8.count <= 160,
              taskID.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || [45,46,58,95].contains($0) }) else {
            throw Failure(message: "后台任务编号无效")
        }
        return "/api/voice/task-status?id=" + taskID
    }

    func run() async throws -> Outcome {
        guard !started else { throw Failure(message: "同一任务监视器不能重复启动") }
        started = true
        var misses = 0, previous = Data()
        for index in 0..<maximumChecks {
            try Task.checkCancellation()
            let bytes: Data
            do { bytes = try await fetch() }
            catch {
                try Task.checkCancellation()
                if error is Failure || error is CancellationError { throw error }
                if index + 1 < maximumChecks { try await delay(kind == .cli ? 2000 : 3000) }
                continue
            }
            try Task.checkCancellation()
            guard bytes.count <= 8 * 1024 * 1024 else { throw Failure(message: "后台任务回执过大") }
            let value = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any]
            guard let value, value["ok"] as? Bool == true else {
                misses += 1
                if kind == .write || misses >= 8 { return .missing }
                if index + 1 < maximumChecks { try await delay(1500) }
                continue
            }
            misses = 0
            let canonical = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
            if canonical != previous {
                // Outside the retry catch: effects with unknown outcomes must
                // never be replayed by opening another GET/delivery cycle.
                try await deliver(canonical)
                try Task.checkCancellation()
                previous = canonical
            }
            let status = value["status"] as? String ?? ""
            if status == "done" || status == "error" || (kind == .write && status != "running") { return .done }
            if index + 1 < maximumChecks { try await delay(kind == .cli ? 1500 : 2000) }
        }
        return .timeout
    }
}
