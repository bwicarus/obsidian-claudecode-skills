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
