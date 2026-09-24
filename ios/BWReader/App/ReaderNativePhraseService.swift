import Foundation

/// One device-level coordinator serializes OCR/index/server projections. A
/// later edit can supersede a queued mirror but cannot be cleared by its ack.
@MainActor
final class ReaderNativePhraseService {
    let deviceID: String
    private let phrases: ReaderNativePhraseStore
    private let vocabulary: ReaderNativeVocabularyState
    private let seedSource: () async throws -> [String]
    private let changed: ([String], [[String: Any]]) -> Void
    private let report: (String) -> Void
    private var seedTask: Task<[String], Error>?
    private var effectsTask: Task<Void, Never>?
    private var projectionTask: Task<Void, Error>?
    private var projectionID = UUID()
    private var projectedRevision: Int64?

    init(store: ReaderNativeDataStore, global: ReaderNativeDataStore, deviceID: String,
         seedSource: @escaping () async throws -> [String],
         changed: @escaping ([String], [[String: Any]]) -> Void, report: @escaping (String) -> Void) {
        self.deviceID = deviceID; self.phrases = .init(store: store, deviceID: deviceID)
        self.vocabulary = .init(store: global, deviceID: deviceID)
        self.seedSource = seedSource; self.changed = changed; self.report = report
    }

    func read() async throws -> [String: Any] {
        var snapshot = try phrases.read()
        if !snapshot.seeded && snapshot.phrases.isEmpty {
            if seedTask == nil { seedTask = Task { try await seedSource() } }
            do {
                let seed = try await seedTask!.value
                seedTask = nil
                snapshot = try phrases.seed(seed)
            } catch { seedTask = nil; throw error }
        }
        // Boot/re-entry refreshes the native tokenizer even when the legacy
        // list predates this service and has no pending effects record.
        try await project(snapshot, changes: [])
        wake()
        return ["ok": true, "phrases": snapshot.phrases, "source": "native-device"]
    }

    func set(_ text: String, enabled: Bool) async throws -> [String: Any] {
        _ = try await read()
        let snapshot = try phrases.set(text, enabled: enabled)
        wake()
        return ["ok": true, "phrases": snapshot.phrases, "source": "native-device", "fav": enabled]
    }

    func wake() {
        guard effectsTask == nil else { return }
        effectsTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.effectsTask = nil }
            // A bounded drain; failed server delivery stays persisted and is
            // retried on the next edit/read/foreground, not a polling timer.
            do {
                while !Task.isCancelled, let pending = try self.phrases.pendingEffects() {
                    guard let revision = pending["revision"] as? Int64,
                          let values = pending["phrases"] as? [String],
                          let changes = pending["changes"] as? [[String: Any]] else {
                        throw ReaderNativeVocabularyState.Failure(message: "词组同步记录损坏")
                    }
                    let snapshot = ReaderNativePhraseStore.Snapshot(phrases: values, seeded: true, revision: revision)
                    try await self.project(snapshot, changes: changes)
                    let mirror = try await ReaderLocalRuntimeServer.requestBridgeMirror([
                        "path": "/reader-phrases", "method": "POST", "body": ["phrases": values]
                    ])
                    guard mirror["ok"] as? Bool == true else { throw ReaderNativeVocabularyState.Failure(message: "词组已保存，服务器镜像待重试") }
                    try self.phrases.completeEffects(revision: revision)
                }
            } catch { if !Task.isCancelled { self.report(error.localizedDescription) } }
        }
    }

    private func project(_ snapshot: ReaderNativePhraseStore.Snapshot, changes: [[String: Any]]) async throws {
        let previous = projectionTask, lease = UUID()
        projectionID = lease
        let task = Task { @MainActor in
            _ = try? await previous?.value
            if let projectedRevision, snapshot.revision < projectedRevision { return }
            try await applyProjection(snapshot, changes: changes)
        }
        projectionTask = task
        defer { if projectionID == lease { projectionTask = nil } }
        try await task.value
    }

    private func applyProjection(_ snapshot: ReaderNativePhraseStore.Snapshot, changes: [[String: Any]]) async throws {
        var records: [[String: Any]] = []
        for item in changes {
            guard let text = item["text"] as? String, let enabled = item["enabled"] as? Bool,
                  let mutation = item["mutation"] as? String else { throw ReaderNativeVocabularyState.Failure(message: "词组更新记录无效") }
            let japanese = text.unicodeScalars.contains { (0x3000...0x9fff).contains($0.value) || (0xff00...0xffef).contains($0.value) }
            records.append(try vocabulary.set(["kind": "phrase", "language": japanese ? "ja" : "en",
                "text": text, "lemma": text], property: "favorite", enabled: enabled, mutation: mutation))
        }
        if projectedRevision == nil || snapshot.revision > projectedRevision! {
            _ = try await NativeBookOCRManager.shared.setPhrases(snapshot.phrases)
            projectedRevision = snapshot.revision
            changed(snapshot.phrases, records)
        } else if !records.isEmpty { changed(snapshot.phrases, records) }
    }
}
