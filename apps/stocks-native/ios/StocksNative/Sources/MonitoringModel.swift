import Combine
import Foundation

@MainActor
final class MonitoringModel: ObservableObject {
    @Published private(set) var library: MonitoringLibrary?
    @Published private(set) var catalog: MonitoringCatalog?
    @Published private(set) var isRefreshing = false
    @Published private(set) var isMutating = false
    @Published private(set) var isFresh = false
    @Published private(set) var pendingMutation: MonitoringMutation?
    @Published private(set) var dismissedBannerIDs: Set<String> = []
    @Published var error: String?
    private var client: APIClient?
    private var scope = "signed-out"
    private var generation = UUID()
    private var libraryEpoch = UUID()
    private var visibleReceipts: Set<String> = []
    private var cacheURL: URL?

    var unreadCount: Int { library?.notifications.filter(\.isUnread).count ?? 0 }
    var accountScope: String { scope }
    var canWrite: Bool { client != nil && isFresh && !isMutating && pendingMutation == nil }
    var banner: MonitoringNotice? {
        library?.notifications.first { $0.isUnread && !dismissedBannerIDs.contains($0.id) }
    }

    func connect(client next: APIClient?) async {
        let nextScope = StockSelectionModel.scopeID(client: next)
        guard nextScope != scope else { return }
        generation = UUID(); libraryEpoch = UUID()
        scope = nextScope; client = next; cacheURL = nil
        library = nil; catalog = nil; error = nil; pendingMutation = nil
        isRefreshing = false; isMutating = false; isFresh = false
        dismissedBannerIDs = []; visibleReceipts = []
        guard next != nil, nextScope != "signed-out" else { return }
        if let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            let folder = base.appendingPathComponent("StocksNative/Monitoring", isDirectory: true)
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            cacheURL = folder.appendingPathComponent(nextScope + ".json")
            if let cacheURL, let data = try? Data(contentsOf: cacheURL),
               let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) {
                library = snapshot.library; pendingMutation = snapshot.pendingMutation
            }
        }
        await refresh()
    }

    func refresh() async {
        guard let client, !isRefreshing, !isMutating else { return }
        let current = generation
        let epoch = libraryEpoch
        isRefreshing = true
        defer { if current == generation { isRefreshing = false } }
        do {
            // The inbox remains usable if only the optional editor catalog fails.
            async let newCatalog = try? client.monitoringCatalog()
            let newLibrary = try await client.monitoringLibrary()
            let receivedCatalog = await newCatalog
            guard current == generation, epoch == libraryEpoch, !Task.isCancelled else { return }
            if let receivedCatalog { catalog = receivedCatalog }
            accept(newLibrary)
            isFresh = true; error = nil
            persist()
        } catch {
            guard current == generation, !Task.isCancelled else { return }
            isFresh = false
            self.error = error.localizedDescription
        }
    }

    @discardableResult
    func mutate(operation: String, rule: MonitoringRule? = nil, id: String? = nil) async -> Bool {
        guard canWrite, let revision = library?.revision else {
            error = pendingMutation == nil ? "请先同步盯盘规则。" : "请先确认上次操作结果。"
            return false
        }
        pendingMutation = .init(requestId: UUID().uuidString, expectedRevision: revision,
                                operation: operation, rule: rule, id: id)
        persist()
        return await retryPending()
    }

    @discardableResult
    func retryPending() async -> Bool {
        guard let client, let mutation = pendingMutation, !isMutating else { return false }
        let current = generation
        libraryEpoch = UUID()
        isMutating = true
        defer { if current == generation { isMutating = false } }
        do {
            let receipt = try await client.mutateMonitoring(mutation)
            guard current == generation else { return false }
            guard receipt.success, receipt.requestId == mutation.requestId else {
                error = "服务器未确认本次操作，请重试确认。"
                return false
            }
            pendingMutation = nil
            accept(receipt.library)
            isFresh = true; error = nil; persist()
            return true
        } catch let failure as MonitoringAPIError {
            guard current == generation else { return false }
            // A definite rejection can be edited and resubmitted. Network ambiguity keeps the same ID.
            if (400..<500).contains(failure.status) {
                pendingMutation = nil; persist()
                if failure.status == 409 { isFresh = false }
            }
            error = failure.message
            return false
        } catch {
            guard current == generation else { return false }
            self.error = "操作结果尚未确认：\(error.localizedDescription)"
            return false
        }
    }

    func dismissBanner(_ id: String) { dismissedBannerIDs.insert(id) }

    func recordVisualReceipt(_ id: String) async {
        guard let client, library?.notifications.contains(where: { $0.id == id }) == true,
              !visibleReceipts.contains(id) else { return }
        let current = generation
        visibleReceipts.insert(id)
        do { try await client.recordMonitoringReceipt(id: id) }
        catch {
            if current == generation { visibleReceipts.remove(id) }
        }
    }

    private func accept(_ next: MonitoringLibrary) {
        guard next.revision >= (library?.revision ?? -1) else { return }
        library = next
    }

    private struct Snapshot: Codable {
        let library: MonitoringLibrary?
        let pendingMutation: MonitoringMutation?
    }

    private func persist() {
        guard let cacheURL, let data = try? JSONEncoder().encode(Snapshot(library: library, pendingMutation: pendingMutation)) else { return }
        try? data.write(to: cacheURL, options: .atomic)
    }
}
