import Combine
import CryptoKit
import Foundation

@MainActor
final class StockSelectionModel: ObservableObject {
    @Published private(set) var catalog: SelectionCatalog?
    @Published private(set) var library: SelectionLibrary?
    @Published private(set) var evaluation: SelectionEvaluation?
    @Published private(set) var watchEvaluation: SelectionEvaluation?
    @Published private(set) var isLoading = false
    @Published private(set) var isEvaluating = false
    @Published private(set) var isLoadingGroup = false
    @Published private(set) var isMutating = false
    @Published private(set) var isLibraryFresh = false
    @Published private(set) var pendingMutation: SelectionMutation?
    @Published private(set) var evaluationDefinition: SelectionDefinition?
    @Published private(set) var lastSavedGroupID: String?
    @Published private(set) var lastSavedPresetID: String?
    @Published private(set) var watchQuoteTime: String?
    @Published var sort = "code"
    @Published var descending = false
    @Published var error: String?
    @Published var notice: String?
    @Published var selectedGroupID: String?
    @Published var selectedPresetID: String?
    @Published var selectedCodes: Set<String> = []
    @Published var draft = SelectionDefinition() {
        didSet { if draft != oldValue { selectedCodes = []; persist() } }
    }
    private var client: APIClient?
    private var scope: String?
    private var generation = UUID()
    private var evaluationGeneration = UUID()
    private var evaluationSort: String?
    private var evaluationDescending: Bool?
    private var groupGeneration = UUID()
    private var cacheURL: URL?
    private var lastSmartRefresh: Date?
    private var lastGroupID: String?
    private var restoringCache = false
    var onContextChange: ((String, String?) -> Void)?

    var manualGroups: [SelectionWatchGroup] { library?.groups.filter { !$0.isSmart } ?? [] }
    var selectedGroup: SelectionWatchGroup? { library?.groups.first { $0.id == selectedGroupID } }
    var selectedPreset: SelectionPreset? { library?.presets.first { $0.id == selectedPresetID } }
    var requiresPresetConfirmation: Bool { selectedPreset?.status == "needs_migration" }
    var resultsAreCurrent: Bool {
        evaluation != nil && evaluationDefinition == draft && evaluationSort == sort
            && evaluationDescending == descending && !requiresPresetConfirmation
    }
    var liveEvaluationKey: String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.nonConformingFloatEncodingStrategy = .convertToString(positiveInfinity: "Infinity", negativeInfinity: "-Infinity", nan: "NaN")
        let definition = (try? encoder.encode(draft))?.base64EncodedString() ?? "invalid"
        let parts = [scope ?? "signed-out", client == nil ? "offline" : "connected",
                     catalog == nil ? "no-catalog" : "catalog-\(catalog?.schemaVersion ?? 0)",
                     selectedPresetID ?? "temporary", selectedPreset?.status ?? "ready",
                     validationMessage ?? "valid", sort, descending ? "descending" : "ascending", definition]
        return SHA256.hash(data: Data(parts.joined(separator: "\n").utf8))
            .map { String(format: "%02x", $0) }.joined()
    }
    var canWrite: Bool { isLibraryFresh && !isMutating && pendingMutation == nil && client != nil }
    var validationMessage: String? {
        for parameter in catalog?.parameters ?? [] {
            guard let value = draft.parameters[parameter.id] else { continue }
            if !value.isFinite || (parameter.minimum.map { value < $0 } ?? false) || (parameter.maximum.map { value > $0 } ?? false) {
                return "请检查“\(parameter.label)”的取值范围。"
            }
            if parameter.type == "integer", value.rounded() != value { return "“\(parameter.label)”需要整数。" }
        }
        return nil
    }

    static func scopeID(client: APIClient?) -> String {
        guard let client, let token = client.token, !token.isEmpty else { return "signed-out" }
        return SHA256.hash(data: Data((client.baseURL.absoluteString + "\n" + token).utf8))
            .map { String(format: "%02x", $0) }.joined()
    }

    func connect(client next: APIClient?) async {
        let nextScope = Self.scopeID(client: next)
        if nextScope == scope { return }
        generation = UUID(); evaluationGeneration = UUID(); groupGeneration = UUID()
        client = next; scope = nextScope; cacheURL = nil
        catalog = nil; library = nil; evaluation = nil; watchEvaluation = nil
        pendingMutation = nil; evaluationDefinition = nil; isLibraryFresh = false
        evaluationSort = nil; evaluationDescending = nil
        selectedCodes = []; selectedGroupID = nil; selectedPresetID = nil
        error = nil; notice = nil; isLoading = false; isEvaluating = false; isLoadingGroup = false; isMutating = false
        lastSmartRefresh = nil; lastGroupID = nil; draft = SelectionDefinition()
        lastSavedGroupID = nil; lastSavedPresetID = nil
        watchQuoteTime = nil; sort = "code"; descending = false
        guard next != nil, nextScope != "signed-out" else { return }
        if let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            let folder = base.appendingPathComponent("StocksNative/Selection", isDirectory: true)
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            cacheURL = folder.appendingPathComponent(nextScope + ".json")
            if let cacheURL, let data = try? Data(contentsOf: cacheURL),
               let cached = try? JSONDecoder().decode(CacheSnapshot.self, from: data), cached.schemaVersion == 1 {
                restoringCache = true
                catalog = cached.catalog; library = cached.library
                draft = cached.draft; pendingMutation = cached.pendingMutation
                selectedGroupID = library?.groups.first?.id
                restoringCache = false
                notice = "显示本机保存的方案，正在同步。"
            }
        }
        await refresh()
    }

    func refresh() async {
        guard let client, !isLoading else { return }
        let current = generation
        isLoading = true
        defer { if current == generation { isLoading = false } }
        do {
            async let requestedCatalog = client.selectionCatalog()
            async let requestedLibrary = client.selectionLibrary()
            let (newCatalog, newLibrary) = try await (requestedCatalog, requestedLibrary)
            guard current == generation, !Task.isCancelled else { return }
            let firstCatalog = catalog == nil
            catalog = newCatalog
            accept(newLibrary)
            if firstCatalog && draft.groups.isEmpty { draft = newCatalog.defaults }
            isLibraryFresh = true; error = nil
            notice = pendingMutation == nil ? nil : "上次操作结果尚未确认，可按原请求重试。"
            persist()
        } catch {
            guard current == generation, !Task.isCancelled else { return }
            self.error = error.localizedDescription
            if library != nil { notice = "当前为本机快照，服务器尚未完成同步。" }
        }
    }

    func refreshFromExternalChange() async {
        await refresh()
        if selectedGroupID != nil { await loadSelectedGroup() }
    }

    func usePreset(_ id: String?) {
        selectedPresetID = id
        if let preset = selectedPreset { draft = preset.definition }
        else if let catalog { draft = catalog.defaults }
        selectedCodes = []
        onContextChange?("screener", selectedPreset?.name ?? "临时方案")
    }

    func run(append: Bool = false, includeHistory: Bool = false) async {
        await evaluate(append: append, includeHistory: includeHistory, recordPresetRun: true)
    }

    func runLive() async {
        let current = generation
        let key = liveEvaluationKey
        do {
            try await Task.sleep(for: .milliseconds(350))
            while isEvaluating {
                guard current == generation, key == liveEvaluationKey, !Task.isCancelled else { return }
                try await Task.sleep(for: .milliseconds(80))
            }
        } catch { return }
        guard current == generation, key == liveEvaluationKey, !Task.isCancelled,
              catalog != nil, validationMessage == nil, !requiresPresetConfirmation,
              !resultsAreCurrent else { return }
        await evaluate(append: false, includeHistory: false, recordPresetRun: false)
    }

    private func evaluate(append: Bool, includeHistory: Bool, recordPresetRun: Bool) async {
        guard let client, !isEvaluating else { return }
        guard !Task.isCancelled, !append || resultsAreCurrent else { return }
        guard !requiresPresetConfirmation else {
            error = "这个旧方案含尚未迁移的条件。请打开“编辑条件”逐项检查，并明确保存确认后再运行。"
            return
        }
        if let validationMessage { error = validationMessage; return }
        let current = generation
        let requestGeneration = UUID(); evaluationGeneration = requestGeneration
        let definition = draft
        let requestedSort = sort
        let requestedDescending = descending
        let requestedKey = liveEvaluationKey
        let offset = append && resultsAreCurrent ? (evaluation?.items.count ?? 0) : 0
        isEvaluating = true; error = nil
        defer { if current == generation && requestGeneration == evaluationGeneration { isEvaluating = false } }
        if recordPresetRun, offset == 0, !includeHistory, requestedSort == "code", !requestedDescending,
           let preset = selectedPreset, preset.definition == definition, canWrite {
            _ = await mutate(operation: "preset.run", payload: SelectionMutationPayload(id: preset.id))
            return
        }
        do {
            var result = try await client.evaluateSelection(SelectionEvaluateRequest(definition: definition, offset: offset,
                                                                                    sort: requestedSort, descending: requestedDescending,
                                                                                    includeHistory: includeHistory))
            guard current == generation, requestGeneration == evaluationGeneration, !Task.isCancelled,
                  requestedKey == liveEvaluationKey, definition == draft,
                  requestedSort == sort, requestedDescending == descending else { return }
            if offset > 0, let previous = evaluation, resultsAreCurrent {
                let existing = Set(previous.items.map(\.code))
                result.items = previous.items + result.items.filter { !existing.contains($0.code) }
            }
            evaluationSort = requestedSort; evaluationDescending = requestedDescending
            evaluation = result; evaluationDefinition = definition
            if offset == 0 { selectedCodes = [] }
            onContextChange?("screener", "\(result.passed) 只符合条件，数据 \(result.asOf ?? "时间未知")")
        } catch {
            guard current == generation, requestGeneration == evaluationGeneration, !Task.isCancelled,
                  requestedKey == liveEvaluationKey, definition == draft,
                  requestedSort == sort, requestedDescending == descending else { return }
            self.error = error.localizedDescription
        }
    }

    func loadSelectedGroup(append: Bool = false) async {
        guard let client, let id = selectedGroupID else { watchEvaluation = nil; return }
        let current = generation
        let requestGeneration = UUID(); groupGeneration = requestGeneration
        let offset = append && lastGroupID == id ? (watchEvaluation?.items.count ?? 0) : 0
        if lastGroupID != id { watchEvaluation = nil; selectedCodes = []; watchQuoteTime = nil; lastSmartRefresh = nil }
        lastGroupID = id; isLoadingGroup = true; error = nil
        defer { if current == generation && requestGeneration == groupGeneration { isLoadingGroup = false } }
        do {
            var result = try await client.evaluateSelection(SelectionEvaluateRequest(offset: offset, groupId: id,
                                                                                    sort: sort, descending: descending))
            guard current == generation, requestGeneration == groupGeneration, selectedGroupID == id, !Task.isCancelled else { return }
            if offset > 0, let previous = watchEvaluation {
                let existing = Set(previous.items.map(\.code))
                result.items = previous.items + result.items.filter { !existing.contains($0.code) }
            }
            watchEvaluation = result
            watchQuoteTime = nil
            onContextChange?("watchlist", "\(selectedGroup?.name ?? "观察池")，\(result.passed) 只，数据 \(result.asOf ?? "时间未知")")
        } catch {
            guard current == generation, requestGeneration == groupGeneration else { return }
            self.error = error.localizedDescription
        }
    }

    func refreshVisibleGroup(force: Bool = false) async {
        guard !isLoadingGroup, !isMutating, pendingMutation == nil, let group = selectedGroup else { return }
        if force || lastSmartRefresh == nil || Date().timeIntervalSince(lastSmartRefresh!) >= Double(group.refreshInterval) {
            await refresh()
            await loadSelectedGroup()
            lastSmartRefresh = Date()
        }
    }

    func refreshVisibleQuotes() async {
        guard let client, let group = selectedGroup, group.realtimeEnabled,
              let result = watchEvaluation, !result.items.isEmpty, !isLoadingGroup else { return }
        let current = generation
        let groupID = group.id
        do {
            let response = try await client.realtime(codes: Array(result.items.prefix(100).map(\.code)))
            guard current == generation, selectedGroupID == groupID, var latest = watchEvaluation else { return }
            let quotes = Dictionary(response.items.map { ($0.code, $0) }, uniquingKeysWith: { _, last in last })
            latest.items = latest.items.map { item in
                guard let quote = quotes[item.code] else { return item }
                return SelectionStock(code: item.code, name: item.name, price: quote.price ?? item.price,
                                      changePct: quote.changePct ?? item.changePct,
                                      turnoverRate: quote.turnoverRate ?? item.turnoverRate, sector: item.sector,
                                      checks: item.checks, passed: item.passed, matchedGroups: item.matchedGroups, score: item.score)
            }
            watchEvaluation = latest
            watchQuoteTime = response.items.compactMap(\.quoteTime).max()
        } catch { /* Keep the dated snapshot; membership refresh reports connection failures. */ }
    }

    @discardableResult
    func mutate(operation: String, payload: SelectionMutationPayload) async -> Bool {
        if operation == "preset.save", let validationMessage { error = validationMessage; return false }
        guard canWrite, let revision = library?.revision else {
            error = pendingMutation == nil ? "请先同步观察池，再保存更改。" : "请先确认上次操作结果。"
            return false
        }
        let mutation = SelectionMutation(requestId: UUID().uuidString, expectedRevision: revision,
                                         operation: operation, payload: payload)
        pendingMutation = mutation; persist()
        return await performPending()
    }

    @discardableResult
    func retryPending() async -> Bool { await performPending() }

    private func performPending() async -> Bool {
        guard let client, let mutation = pendingMutation, !isMutating else { return false }
        let current = generation
        let requestGeneration = evaluationGeneration
        isMutating = true; error = nil
        defer { if current == generation { isMutating = false } }
        do {
            let receipt = try await client.mutateSelection(mutation)
            guard current == generation else { return false }
            guard receipt.requestId == mutation.requestId, receipt.success,
                  receipt.revision == receipt.library.revision else {
                throw AppError.message("服务器未返回有效保存回执，请重试确认结果。")
            }
            accept(receipt.library); isLibraryFresh = true; pendingMutation = nil
            lastSavedGroupID = receipt.groupId; lastSavedPresetID = receipt.presetId
            if mutation.operation == "preset.run", let result = receipt.evaluation,
               let appliedDefinition = receipt.library.presets.first(where: { $0.id == mutation.payload.id })?.definition,
               requestGeneration == evaluationGeneration, !Task.isCancelled,
               selectedPresetID == mutation.payload.id, appliedDefinition == draft,
               sort == "code", !descending, !requiresPresetConfirmation {
                evaluationSort = "code"; evaluationDescending = false
                evaluation = result; evaluationDefinition = appliedDefinition
            }
            notice = receipt.replayed == true ? "已确认上次操作已保存。" : "已保存并同步。"
            persist()
            if mutation.operation.hasPrefix("group."), selectedGroupID != nil { await loadSelectedGroup() }
            onContextChange?("selection_library", "观察池与选股方案已更新")
            return true
        } catch let failure as SelectionAPIError {
            guard current == generation else { return false }
            if failure.status == 409 && (failure.code == "revision_conflict" || failure.code == nil) {
                pendingMutation = nil; persist()
                await refresh()
                error = "其他设备或 AI 已更新观察池。已同步最新内容；你的编辑仍保留，请检查后重新保存。"
            } else if (400..<500).contains(failure.status) && failure.status != 408 && failure.status != 429 {
                pendingMutation = nil; persist(); error = failure.localizedDescription
                if failure.status == 401 || failure.status == 403 { isLibraryFresh = false }
            } else {
                error = "保存结果暂未确认：\(failure.localizedDescription)。请重试确认。"
            }
        } catch {
            guard current == generation else { return false }
            self.error = "保存结果暂未确认：\(error.localizedDescription)。请重试确认。"
        }
        return false
    }

    private func accept(_ incoming: SelectionLibrary) {
        if let existing = library, incoming.revision < existing.revision { return }
        library = incoming
        if !incoming.groups.contains(where: { $0.id == selectedGroupID }) {
            selectedGroupID = incoming.groups.first?.id; watchEvaluation = nil
        }
        if let selectedPresetID, !incoming.presets.contains(where: { $0.id == selectedPresetID }) {
            self.selectedPresetID = nil
        }
    }

    func setCriterion(groupID: String, criterionID: String, excluded: Bool, present: Bool) {
        guard let index = draft.groups.firstIndex(where: { $0.id == groupID }) else { return }
        let key = groupID + "|" + (excluded ? "not:" : "") + criterionID
        if excluded {
            draft.groups[index].not.removeAll { $0 == criterionID }
            if present { draft.groups[index].not.append(criterionID) }
        } else {
            draft.groups[index].and.removeAll { $0 == criterionID }
            if present { draft.groups[index].and.append(criterionID) }
        }
        draft.disabled.removeAll { $0 == key }
    }

    func setCriterionEnabled(groupID: String, criterionID: String, excluded: Bool, enabled: Bool) {
        let key = groupID + "|" + (excluded ? "not:" : "") + criterionID
        draft.disabled.removeAll { $0 == key }
        if !enabled { draft.disabled.append(key) }
    }

    func removeRuleGroup(_ id: String) {
        draft.groups.removeAll { $0.id == id }
        draft.disabled.removeAll { $0.hasPrefix(id + "|") }
    }

    private struct CacheSnapshot: Codable {
        var schemaVersion = 1
        let catalog: SelectionCatalog?
        let library: SelectionLibrary?
        let draft: SelectionDefinition
        let pendingMutation: SelectionMutation?
    }

    private func persist() {
        guard !restoringCache, let cacheURL,
              let encoded = try? JSONEncoder().encode(CacheSnapshot(catalog: catalog, library: library,
                                                                    draft: draft, pendingMutation: pendingMutation)) else { return }
        try? encoded.write(to: cacheURL, options: .atomic)
    }
}
