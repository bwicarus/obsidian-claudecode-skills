import Foundation

actor Harness {
    var reads = 0, deliveries: [Data] = [], delays: [UInt64] = []
    let scenario: String
    init(_ scenario: String) { self.scenario = scenario }
    func read() throws -> Data {
        reads += 1
        if scenario == "cancel" { throw CancellationError() }
        if scenario == "retry" && reads == 1 { throw URLError(.networkConnectionLost) }
        if scenario == "missing" { return Data(#"{"ok":false}"#.utf8) }
        if reads < 3 { return Data(#"{"ok":true,"status":"running","step":"working","client_actions":[{"fn":"one"}]}"#.utf8) }
        return Data(#"{"ok":true,"status":"done","result":{"undo_id":"undo-one"},"client_actions":[{"fn":"one"},{"fn":"two"}]}"#.utf8)
    }
    func deliver(_ data: Data) throws {
        deliveries.append(data)
        if scenario == "unknown-delivery" { throw URLError(.cannotParseResponse) }
    }
    func delay(_ value: UInt64) { delays.append(value) }
    func verify(_ outcome: ReaderNativeTaskMonitor.Outcome?) {
        switch scenario {
        case "cancel": precondition(reads == 1 && deliveries.isEmpty && delays.isEmpty)
        case "unknown-delivery": precondition(reads == 1 && deliveries.count == 1 && delays.isEmpty)
        case "missing": precondition(reads == 8 && deliveries.isEmpty && delays.count == 7 && outcome == .missing)
        default:
            precondition(reads == 3 && deliveries.count == 2 && outcome == .done, "duplicate snapshot or terminal query")
            if scenario == "retry" { precondition(delays.first == 2000) }
        }
    }
}

@main struct Test {
    static func main() async throws {
        let path = try ReaderNativeTaskMonitor.path(taskID: "task-123.a:4")
        precondition(path == "/api/voice/task-status?id=task-123.a:4")
        for id in ["", "../other", "a&b=1", String(repeating: "a", count:161)] {
            do { _ = try ReaderNativeTaskMonitor.path(taskID:id); preconditionFailure("invalid task ID accepted") }
            catch is ReaderNativeTaskMonitor.Failure {}
        }
        for scenario in ["normal", "retry", "missing", "cancel", "unknown-delivery"] {
            let harness = Harness(scenario)
            let monitor = ReaderNativeTaskMonitor(kind:.cli, maximumChecks:10,
                fetch:{try await harness.read()}, deliver:{try await harness.deliver($0)}, delay:{await harness.delay($0)})
            var outcome: ReaderNativeTaskMonitor.Outcome?
            do { outcome = try await monitor.run(); precondition(scenario != "cancel" && scenario != "unknown-delivery") }
            catch { precondition(scenario == "cancel" || scenario == "unknown-delivery") }
            await harness.verify(outcome)
            do { _ = try await monitor.run(); preconditionFailure("watcher restarted") }
            catch is ReaderNativeTaskMonitor.Failure {}
        }
        let bounded = ReaderNativeTaskMonitor(kind:.write, maximumChecks:2,
            fetch:{ Data(#"{"ok":true,"status":"running"}"#.utf8) }, deliver:{_ in}, delay:{_ in})
        let outcome = try await bounded.run()
        precondition(outcome == .timeout)
        print("Native task tracking: terminal stop, unchanged snapshot, retry, missing task, cancellation and unknown delivery passed")
    }
}
