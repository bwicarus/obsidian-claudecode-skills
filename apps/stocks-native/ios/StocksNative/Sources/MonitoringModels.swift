import Foundation

struct MonitoringMetric: Decodable, Identifiable {
    let id: String
    let label: String
    let unit: String?

    static let defaults: [MonitoringMetric] = [
        .init(id: "price", label: "最新价", unit: "元"),
        .init(id: "changePct", label: "涨跌幅", unit: "%"),
        .init(id: "volumeRatio", label: "量比", unit: "倍"),
        .init(id: "turnoverRate", label: "换手率", unit: "%"),
        .init(id: "amplitude", label: "振幅", unit: "%"),
        .init(id: "nearLimitPct", label: "距涨停", unit: "%"),
    ]
}

struct MonitoringCatalog: Decodable {
    let metrics: [MonitoringMetric]
}

struct MonitoringCondition: Codable, Equatable {
    var metric: String
    var op: String
    var threshold: Double

    var display: String {
        let metricInfo = MonitoringMetric.defaults.first { $0.id == metric }
        let operation = ["above": "≥", "below": "≤"][op] ?? op
        return "\(metricInfo?.label ?? metric) \(operation) \(threshold.formatted(.number.precision(.fractionLength(0...4))))\(metricInfo?.unit ?? "")"
    }
}

struct MonitoringRule: Codable, Identifiable, Equatable {
    var id: String
    var title: String
    var code: String
    var conditions: [MonitoringCondition]
    var match: String
    var confirmSeconds: Double
    var cooldownSeconds: Double
    var rearmPercent: Double
    var severity: String
    var enabled: Bool
    var state: String?
    var lastEventAt: String?

    static func draft(code: String = "") -> MonitoringRule {
        .init(id: UUID().uuidString, title: "", code: code,
              conditions: [.init(metric: "price", op: "above", threshold: 0)],
              match: "all", confirmSeconds: 10, cooldownSeconds: 300,
              rearmPercent: 0.05, severity: "normal", enabled: true)
    }

    var conditionDescription: String {
        conditions.map(\.display).joined(separator: match == "any" ? "；或 " : "；且 ")
    }

    var stateTitle: String {
        if !enabled { return "已暂停" }
        return MonitoringSummary.title(for: state ?? "watching")
    }
}

struct MonitoringNotice: Codable, Identifiable, Equatable {
    let id: String
    let ruleId: String?
    let code: String?
    let title: String
    let body: String
    let severity: String
    let aiState: String
    let createdAt: String
    let status: String

    var isUnread: Bool { status == "unread" }
    var isResolved: Bool { status == "resolved" }
    var stockCode: String? { code.flatMap { $0.isEmpty ? nil : $0 } }
    var statusTitle: String { ["unread": "未读", "read": "已读", "resolved": "已处理"][status] ?? status }
    var aiTitle: String? {
        switch aiState {
        case "pending": return "AI 等待分析"
        case "running": return "AI 分析中"
        case "complete": return "AI 已分析"
        case "failed": return "AI 分析未完成，触发记录已保留"
        default: return nil
        }
    }
    var displayTime: String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = formatter.date(from: createdAt) ?? ISO8601DateFormatter().date(from: createdAt)
        return date?.formatted(date: .abbreviated, time: .shortened) ?? createdAt
    }
}

struct MonitoringSummary: Codable, Equatable {
    let ruleCount: Int
    let enabledCount: Int
    let unreadCount: Int
    let state: String
    let lastEventAt: String?

    static func title(for state: String) -> String {
        ["watching": "监测中", "armed": "监测中", "active": "监测中", "idle": "待监测",
         "confirming": "确认信号", "pending": "等待行情", "waiting_data": "等待行情",
         "waiting": "等待行情", "market_closed": "休市", "data_unavailable": "行情缺失",
         "stale": "行情待更新", "cooldown": "冷却中", "triggered": "已触发",
         "paused": "已暂停", "disabled": "已暂停", "error": "监测异常"][state] ?? state
    }

    var title: String {
        if unreadCount > 0 { return "\(unreadCount) 条提醒" }
        if ruleCount == 0 { return "通知记录" }
        if enabledCount == 0 { return "已暂停 · \(ruleCount)" }
        return "\(Self.title(for: state)) · \(enabledCount)"
    }
}

struct MonitoringLibrary: Codable {
    let revision: Int
    let rules: [MonitoringRule]
    let notifications: [MonitoringNotice]
    let summary: [String: MonitoringSummary]
}

struct MonitoringMutation: Codable {
    let requestId: String
    let expectedRevision: Int?
    let operation: String
    let rule: MonitoringRule?
    let id: String?
}

struct MonitoringMutationReceipt: Decodable {
    let success: Bool
    let requestId: String
    let revision: Int
    let library: MonitoringLibrary
}

struct MonitoringAPIError: LocalizedError {
    let status: Int
    let message: String
    var errorDescription: String? { message }
}

struct MonitoringDeliveryReceipt: Encodable {
    let notificationId: String
    let channel: String
    let outcome: String
}

struct MonitoringDeliveryResponse: Decodable {}

struct MonitoringDestination: Identifiable {
    let id = UUID()
    var code: String?
    var notificationId: String?
    var tab: String = "notifications"
}
