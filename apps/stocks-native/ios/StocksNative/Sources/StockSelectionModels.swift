import Foundation

enum StockSelectionSection: String, CaseIterable, Identifiable {
    case market, watchlist, screener
    var id: String { rawValue }
    var title: String {
        switch self { case .market: return "市场"; case .watchlist: return "观察池"; case .screener: return "选股" }
    }
}

struct SelectionCriterion: Codable, Identifiable {
    let id: String
    let label: String
    let category: String?
    let parameters: [String]?
    let description: String?
}

struct SelectionParameter: Codable, Identifiable {
    let id: String
    let label: String
    let type: String?
    let minimum: Double?
    let maximum: Double?
    let defaultValue: Double?
    enum CodingKeys: String, CodingKey { case id, label, type, minimum, maximum; case defaultValue = "default" }
}

struct SelectionSmartAttribute: Codable, Identifiable {
    let id: String
    let label: String
    let status: String?
}

struct SelectionCatalog: Codable {
    let schemaVersion: Int
    let criteria: [SelectionCriterion]
    let parameters: [SelectionParameter]
    let defaults: SelectionDefinition
    let smartAttributes: [SelectionSmartAttribute]?
}

struct SelectionRuleGroup: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var and: [String]
    var not: [String]
    var enabled: Bool

    init(id: String = UUID().uuidString, name: String = "条件组", and: [String] = [], not: [String] = [], enabled: Bool = true) {
        self.id = id; self.name = name; self.and = and; self.not = not; self.enabled = enabled
    }
    enum CodingKeys: String, CodingKey { case id, name, and, not, enabled }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        name = try values.decodeIfPresent(String.self, forKey: .name) ?? "条件组"
        and = try values.decodeIfPresent([String].self, forKey: .and) ?? []
        not = try values.decodeIfPresent([String].self, forKey: .not) ?? []
        enabled = try values.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
    }
}

struct SelectionDefinition: Codable, Equatable {
    var groups: [SelectionRuleGroup]
    var parameters: [String: Double]
    var disabled: [String]

    init(groups: [SelectionRuleGroup] = [], parameters: [String: Double] = [:], disabled: [String] = []) {
        self.groups = groups; self.parameters = parameters; self.disabled = disabled
    }
    enum CodingKeys: String, CodingKey { case groups, parameters, disabled }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        groups = try values.decodeIfPresent([SelectionRuleGroup].self, forKey: .groups) ?? []
        parameters = try values.decodeIfPresent([String: Double].self, forKey: .parameters) ?? [:]
        disabled = try values.decodeIfPresent([String].self, forKey: .disabled) ?? []
    }
}

struct SelectionCriterionTransfer: Codable {
    let criterionID: String
    let sourceGroupID: String?
    let sourceExcluded: Bool
    private static let prefix = "stocks-criterion:v1:"
    private static let maximumLength = 2048

    init(criterionID: String, sourceGroupID: String? = nil, sourceExcluded: Bool = false) {
        self.criterionID = criterionID
        self.sourceGroupID = sourceGroupID
        self.sourceExcluded = sourceExcluded
    }

    var stringValue: String {
        guard isValid else { return "" }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(self), let json = String(data: data, encoding: .utf8) else { return "" }
        let result = Self.prefix + json
        return result.utf8.count <= Self.maximumLength ? result : ""
    }

    static func decode(_ value: String) -> Self? {
        guard value.utf8.count <= maximumLength, value.hasPrefix(prefix),
              let transfer = try? JSONDecoder().decode(Self.self, from: Data(value.dropFirst(prefix.count).utf8)),
              transfer.isValid else { return nil }
        return transfer
    }

    fileprivate var isValid: Bool {
        Self.validIdentifier(criterionID)
            && (sourceGroupID.map(Self.validIdentifier) ?? !sourceExcluded)
    }

    fileprivate static func validIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128 && value.utf8.allSatisfy {
            (65...90).contains($0) || (97...122).contains($0) || (48...57).contains($0)
                || $0 == 95 || $0 == 46 || $0 == 45
        }
    }
}

extension SelectionDefinition {
    @discardableResult
    mutating func applyCriterionDrop(_ transfer: SelectionCriterionTransfer, targetGroupID: String,
                                     excluded: Bool, allowedIDs: Set<String>) -> Bool {
        let id = transfer.criterionID
        let targets = groups.indices.filter { groups[$0].id == targetGroupID }
        guard transfer.isValid, allowedIDs.contains(id), SelectionCriterionTransfer.validIdentifier(targetGroupID),
              targets.count == 1, let targetIndex = targets.first else { return false }
        let sourceIndex: Int?
        if let sourceID = transfer.sourceGroupID {
            let sources = groups.indices.filter { groups[$0].id == sourceID }
            guard sources.count == 1, let index = sources.first,
                  (transfer.sourceExcluded ? groups[index].not : groups[index].and).contains(id) else { return false }
            sourceIndex = index
        } else { sourceIndex = nil }

        func key(_ groupID: String, _ excluded: Bool) -> String { groupID + "|" + (excluded ? "not:" : "") + id }
        let targetKey = key(targetGroupID, excluded)
        let destinationPosition = (excluded ? groups[targetIndex].not : groups[targetIndex].and).firstIndex(of: id)
        let disabledPosition = disabled.firstIndex(of: targetKey)
        let preserveDisabled: Bool
        if let sourceIndex {
            preserveDisabled = disabled.contains(key(groups[sourceIndex].id, transfer.sourceExcluded))
                || (sourceIndex != targetIndex && !groups[sourceIndex].enabled)
        } else {
            // Reusing a pool item keeps an existing destination item's switch state.
            preserveDisabled = (groups[targetIndex].and.contains(id) && disabled.contains(key(targetGroupID, false)))
                || (groups[targetIndex].not.contains(id) && disabled.contains(key(targetGroupID, true)))
        }

        var next = self
        var affectedKeys = Set([key(targetGroupID, false), key(targetGroupID, true)])
        if let sourceIndex {
            next.groups[sourceIndex].and.removeAll { $0 == id }
            next.groups[sourceIndex].not.removeAll { $0 == id }
            affectedKeys.insert(key(groups[sourceIndex].id, false))
            affectedKeys.insert(key(groups[sourceIndex].id, true))
        }
        next.groups[targetIndex].and.removeAll { $0 == id }
        next.groups[targetIndex].not.removeAll { $0 == id }
        if excluded {
            let position = min(destinationPosition ?? next.groups[targetIndex].not.count, next.groups[targetIndex].not.count)
            next.groups[targetIndex].not.insert(id, at: position)
        } else {
            let position = min(destinationPosition ?? next.groups[targetIndex].and.count, next.groups[targetIndex].and.count)
            next.groups[targetIndex].and.insert(id, at: position)
        }
        next.disabled.removeAll { affectedKeys.contains($0) }
        if preserveDisabled {
            let position = disabledPosition ?? disabled.firstIndex { affectedKeys.contains($0) } ?? next.disabled.count
            next.disabled.insert(targetKey, at: min(position, next.disabled.count))
        }
        self = next
        return true
    }
}

struct SelectionStock: Codable, Identifiable {
    let code: String
    let name: String
    let price: Double?
    let changePct: Double?
    let turnoverRate: Double?
    let sector: String?
    let checks: [String: Bool?]?
    let passed: Bool?
    let matchedGroups: [String]?
    let score: Double?
    var volumeRatio: Double? = nil
    var turnover: Double? = nil
    var marketCap: Double? = nil
    var amplitude: Double? = nil
    var id: String { code }
}

struct SelectionGroupEffect: Codable, Identifiable {
    let id: String
    let name: String?
    let baselinePassed: Int?
    let impacts: [String: Int]?
    let notImpacts: [String: Int]?
    let unknown: Int?
    let enabled: Bool?
}

struct SelectionEvaluation: Codable {
    let asOf: String?
    let source: String?
    let total: Int
    let passed: Int
    let unknown: Int?
    var items: [SelectionStock]
    let groups: [SelectionGroupEffect]?
    let warnings: [String]?
    let unfiltered: Bool?
    let matched: Int?
    let history: SelectionHistory?
    var resultCount: Int { matched ?? passed }
}

struct SelectionEvaluateRequest: Codable {
    let groups: [SelectionRuleGroup]?
    let parameters: [String: Double]?
    let disabled: [String]?
    let query: String?
    let limit: Int
    let offset: Int
    let groupId: String?
    let presetId: String?
    let sort: String?
    let descending: Bool?
    let includeHistory: Bool?

    init(definition: SelectionDefinition? = nil, query: String? = nil, limit: Int = 100, offset: Int = 0,
         groupId: String? = nil, presetId: String? = nil, sort: String? = nil, descending: Bool? = nil, includeHistory: Bool? = nil) {
        groups = definition?.groups; parameters = definition?.parameters; disabled = definition?.disabled
        self.query = query; self.limit = limit; self.offset = offset; self.groupId = groupId; self.presetId = presetId
        self.sort = sort; self.descending = descending
        self.includeHistory = includeHistory
    }
}

struct SelectionHistory: Codable {
    let asOf: String?
    let days: Int
    let groups: [SelectionHistoryGroup]
    let status: String?
}

struct SelectionHistoryGroup: Codable, Identifiable {
    let id: String
    let name: String
    let sampleDays: Int
    let hits: Int
    let averageHits: Double?
    let meanExcessPct: Double?
    let ic: Double?
}

struct SelectionSmartRules: Codable, Equatable {
    var match: String
    var attrs: [String]
    var limit: Int
    var definition: SelectionDefinition?
    init(match: String = "all", attrs: [String] = [], limit: Int = 50, definition: SelectionDefinition? = nil) {
        self.match = match; self.attrs = attrs; self.limit = limit; self.definition = definition
    }
}

/// Settings may gain server-defined keys; preserve them when renaming a group.
enum SelectionJSONValue: Codable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: SelectionJSONValue]), array([SelectionJSONValue]), null
    init(from decoder: Decoder) throws {
        let box = try decoder.singleValueContainer()
        if box.decodeNil() { self = .null }
        else if let value = try? box.decode(Bool.self) { self = .bool(value) }
        else if let value = try? box.decode(Double.self) { self = .number(value) }
        else if let value = try? box.decode(String.self) { self = .string(value) }
        else if let value = try? box.decode([String: SelectionJSONValue].self) { self = .object(value) }
        else { self = .array(try box.decode([SelectionJSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var box = encoder.singleValueContainer()
        switch self {
        case .string(let value): try box.encode(value)
        case .number(let value): try box.encode(value)
        case .bool(let value): try box.encode(value)
        case .object(let value): try box.encode(value)
        case .array(let value): try box.encode(value)
        case .null: try box.encodeNil()
        }
    }
}

struct SelectionWatchGroup: Codable, Identifiable {
    let id: String
    let name: String
    let kind: String
    let codes: [String]?
    let rules: SelectionSmartRules?
    let settings: [String: SelectionJSONValue]?
    let status: String?
    let warnings: [String]?
    var isSmart: Bool { kind == "smart" }
    var realtimeEnabled: Bool { if case .bool(let value)? = settings?["realtimeEnabled"] { return value }; return true }
    var realtimeInterval: Int { if case .number(let value)? = settings?["realtimeIntervalSec"] { return min(60, max(3, Int(value))) }; return 5 }
    var refreshInterval: Int { if case .number(let value)? = settings?["refreshIntervalSec"] { return min(3600, max(10, Int(value))) }; return 60 }
}

struct SelectionPreset: Codable, Identifiable {
    let id: String
    let name: String
    let definition: SelectionDefinition
    let status: String?
    let warnings: [String]?
}

struct SelectionLibrary: Codable {
    let revision: Int
    let groups: [SelectionWatchGroup]
    let presets: [SelectionPreset]
    let asOf: String?
    let ownerKey: String?
}

struct SelectionMutationPayload: Codable {
    var id: String?
    var ids: [String]?
    var groupIds: [String]?
    var name: String?
    var kind: String?
    var codes: [String]?
    var rules: SelectionSmartRules?
    var settings: [String: SelectionJSONValue]?
    var definition: SelectionDefinition?
}

struct SelectionMutation: Codable {
    let requestId: String
    let expectedRevision: Int
    let operation: String
    let payload: SelectionMutationPayload
}

struct SelectionMutationReceipt: Decodable {
    let success: Bool
    let revision: Int
    let requestId: String
    let replayed: Bool?
    let library: SelectionLibrary
    let evaluation: SelectionEvaluation?
    let groupId: String?
    let presetId: String?
}

struct SelectionAPIError: LocalizedError {
    let status: Int
    let code: String?
    let message: String
    let revision: Int?
    var errorDescription: String? { message }
}
