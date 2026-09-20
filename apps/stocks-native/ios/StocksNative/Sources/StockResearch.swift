import SwiftUI

struct StockReport: Codable, Identifiable {
    let id: String
    let code: String
    let title: String
    let summary: String
    let direction: String
    let confidence: String
    let points: [String]
    let risks: [String]
    let basis: StockPlanBasis
    let sources: [ResearchSource]
    let source: StockPlanSource
    let createdAt: String
    let planId: String?
    let plan: StockPlan?
    let adoption: PlanAdoption?
}

struct ResearchSource: Codable {
    let title: String
    let url: String?
    let asOf: String?
}

struct PlanAdoption: Codable, Identifiable {
    let id: String
    let planId: String
    let variantId: String
    let ruleIds: [String]
    let state: String
    let enabledCount: Int
    let ruleCount: Int
    let completionStatus: String?
    var label: String {
        switch state {
        case "active", "watching", "enabled": return "盯盘中 · \(enabledCount)"
        case "paused": return "已暂停"
        case "partially_paused": return "部分暂停"
        case "triggered": return "已触发"
        case "partial", "incomplete": return "部分启用"
        case "missing", "deleted", "removed": return "规则已移除"
        case "resting", "market_closed": return "休市待监控"
        default: return enabledCount > 0 ? "盯盘中 · \(enabledCount)" : "未运行"
        }
    }
}

struct PlanActivationPreview: Codable {
    let revision: Int
    let planId: String
    let variantId: String
    let canApply: Bool
    let stale: Bool
    let adoption: PlanAdoption?
    let unsupported: [String]
}

struct PlanActivationReceipt: Codable {
    let success: Bool
    let adoption: PlanAdoption
}

struct PlanActivationRequest: Codable {
    let requestId: String
    let expectedRevision: Int
    let planId: String
    let variantId: String
}

struct ReportListResponse: Codable {
    let revision: Int
    let items: [StockReport]
    let nextCursor: String?
}

struct ReportItemResponse: Codable { let report: StockReport }
struct ReportSignalsResponse: Codable { let items: [String: ResearchSignal] }
struct ResearchSignal: Codable {
    let code: String
    let reportId: String?
    let planId: String?
    let action: String?
    let targetPrice: Double?
    let hasStrategy: Bool
    let planStatus: String?
    let direction: String
    let confidence: String
    let createdAt: String
    let marketAsOf: String
    let adoption: PlanAdoption?
}

struct LegacySignalResponse: Codable { let items: [LegacyStockSignal]; let warnings: [String] }
struct LegacyStockSignal: Codable, Identifiable {
    let id: String
    let code: String
    let kind: String
    let occurredAt: String?
    let title: String
    let summary: String
}

enum ResearchStyle {
    static func direction(_ value: String) -> String {
        ["bullish": "偏多", "neutral": "观察", "bearish": "偏空", "unrated": "未评级"][value] ?? value
    }
    static func confidence(_ value: String) -> String {
        ["low": "低", "medium": "中", "high": "高", "unrated": "未评级"][value] ?? value
    }
    static func date(_ value: String) -> String {
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: value) {
            return date.formatted(.dateTime.year().month().day().hour().minute())
        }
        return StockChartLabels.detail(value)
    }
    static func safeURL(_ value: String?) -> URL? {
        guard let value, let url = URL(string: value), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil else { return nil }
        return url
    }
}

@MainActor
final class StockResearchStore: ObservableObject {
    @Published private(set) var reports: [StockReport] = []
    @Published private(set) var plans: [StockPlan] = []
    @Published private(set) var signals: [String: ResearchSignal] = [:]
    @Published private(set) var adoptions: [String: PlanAdoption] = [:]
    @Published private(set) var busyPlans: Set<String> = []
    @Published private(set) var error: String?
    @Published private(set) var isLoading = false
    @Published private(set) var nextCursors: [String: String] = [:]
    private var client: APIClient?
    private var scope = ""
    private var generation = UUID()
    private var pending: [String: PlanActivationRequest] = [:]
    private var loads: [String: UUID] = [:]

    func configure(client: APIClient?, scope: String) {
        self.client = client
        guard self.scope != scope else { return }
        self.scope = scope; generation = UUID()
        reports = []; plans = []; signals = [:]; adoptions = [:]; busyPlans = []; pending = [:]
        error = nil; loads = [:]; nextCursors = [:]; isLoading = false
    }

    private func merge(_ values: [StockReport]) {
        var map = Dictionary(uniqueKeysWithValues: reports.map { ($0.id, $0) })
        for report in values {
            map[report.id] = report
            if let adoption = report.adoption { adoptions[adoption.planId] = adoption }
        }
        reports = Array(map.values.sorted { $0.createdAt > $1.createdAt }.prefix(500))
        mergePlans(values.compactMap(\.plan))
    }

    func mergePlans(_ values: [StockPlan]) {
        var map = Dictionary(uniqueKeysWithValues: plans.map { ($0.id, $0) })
        for plan in values {
            if let current = map[plan.id] {
                if current.revision > plan.revision { continue }
                if current.revision == plan.revision, current.source.turnId != nil, plan.source.turnId == nil { continue }
            }
            map[plan.id] = plan
        }
        plans = Array(map.values.sorted { $0.createdAt > $1.createdAt }.prefix(500))
    }

    func refresh(code: String? = nil, more: Bool = false) async {
        guard let client else { return }
        let token = generation, key = code ?? "*", ticket = UUID()
        loads[key] = ticket; isLoading = true
        defer { if token == generation, loads[key] == ticket { loads.removeValue(forKey: key); isLoading = !loads.isEmpty } }
        do {
            let result = try await client.reports(code: code, before: more ? nextCursors[key] : nil)
            guard token == generation, loads[key] == ticket else { return }
            merge(result.items); nextCursors[key] = result.nextCursor; error = nil
            if let code {
                let saved = try await client.plans(code: code, includeArchived: true)
                guard token == generation, loads[key] == ticket else { return }
                mergePlans(saved.items)
            }
            let updated = try await client.researchSignals()
            guard token == generation, loads[key] == ticket else { return }
            signals = updated.items
            for value in updated.items.values { if let adoption = value.adoption { adoptions[adoption.planId] = adoption } }
        } catch {
            guard token == generation else { return }
            self.error = error.localizedDescription
        }
    }

    func changed(id: String?, code: String?) async {
        guard let client else { return }
        let token = generation
        if let id, let result = try? await client.report(id: id), token == generation { merge([result.report]) }
        guard token == generation else { return }
        await refresh(code: code)
    }

    func activate(_ plan: StockPlan, variant: StockPlanVariant) async {
        guard let client, !busyPlans.contains(plan.id) else { return }
        let token = generation
        busyPlans.insert(plan.id)
        defer { if token == generation { busyPlans.remove(plan.id) } }
        do {
            let preview = try await client.previewPlan(planID: plan.id, variantID: variant.id)
            guard token == generation else { return }
            if let existing = preview.adoption {
                adoptions[plan.id] = existing
                if existing.variantId == variant.id, existing.completionStatus != "partial" && existing.state != "partial" {
                    error = nil; return
                }
            }
            guard preview.canApply else {
                error = preview.stale ? "这份方案的行情依据已过期，请让助手更新分析。" :
                    (preview.unsupported.isEmpty ? "此档方案暂不能启用盯盘。" : "当前不能执行：" + preview.unsupported.joined(separator: "、"))
                return
            }
            if pending[plan.id]?.variantId != variant.id { pending[plan.id] = nil }
            if pending[plan.id] == nil {
                pending[plan.id] = .init(requestId: UUID().uuidString, expectedRevision: preview.revision,
                                       planId: plan.id, variantId: variant.id)
            }
            guard let request = pending[plan.id] else { return }
            let result = try await client.activatePlan(request)
            guard token == generation else { return }
            adoptions[plan.id] = result.adoption
            guard result.success else { error = "盯盘尚未完整启用，可重试完成剩余规则。"; return }
            pending[plan.id] = nil; adoptions[plan.id] = result.adoption; error = nil
            NotificationCenter.default.post(name: .stocksSelectionDidChange, object: nil)
            await refresh(code: plan.code)
        } catch let failure as StockPlanAPIError {
            guard token == generation else { return }
            if (400..<500).contains(failure.status) { pending[plan.id] = nil }
            error = failure.message
        } catch { if token == generation { self.error = error.localizedDescription } }
    }
}

struct StockReportCard: View {
    let report: StockReport
    @ObservedObject var research: StockResearchStore
    @State private var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(report.title).font(.headline)
                Spacer(minLength: 4)
                Text(report.code).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Text(ResearchStyle.direction(report.direction)).foregroundStyle(AppStyle.accent)
                Text("置信度 \(ResearchStyle.confidence(report.confidence))")
                Spacer(minLength: 0)
            }.font(.caption)
            Text(ResearchStyle.date(report.createdAt)).font(.caption2).foregroundStyle(.secondary)
            Text(report.summary).font(.subheadline).textSelection(.enabled)
            DisclosureGroup("依据与风险", isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("行情时间 · \(ResearchStyle.date(report.basis.marketAsOf))")
                    ForEach(Array(report.points.enumerated()), id: \.offset) { _, point in Text("• " + point) }
                    ForEach(Array(report.risks.enumerated()), id: \.offset) { _, risk in
                        Label(risk, systemImage: "exclamationmark.circle").foregroundStyle(.orange)
                    }
                    ForEach(Array(report.sources.enumerated()), id: \.offset) { _, source in
                        if let url = ResearchStyle.safeURL(source.url) { Link(source.title, destination: url) }
                        else { Text(source.title).foregroundStyle(.secondary) }
                    }
                }.font(.caption).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 6)
            }.font(.caption).tint(AppStyle.accent)
            if let plan = report.plan {
                ResearchPlanCard(plan: plan, research: research)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(.white, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct ResearchPlanCard: View {
    let plan: StockPlan
    @ObservedObject var research: StockResearchStore
    var isArchiving = false
    var onArchive: (() -> Void)? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            let adoption = research.adoptions[plan.id]
            let incomplete = adoption?.state == "partial" || adoption?.completionStatus == "partial"
            StockPlanCard(plan: plan, isArchiving: isArchiving, onArchive: onArchive,
                          onChoose: { variant in Task { await research.activate(plan, variant: variant) } },
                          activationBusy: research.busyPlans.contains(plan.id),
                          adoptedVariantID: incomplete ? nil : adoption?.variantId)
            if let adoption = research.adoptions[plan.id] {
                Label(adoption.label, systemImage: "waveform.path.ecg").font(.caption).foregroundStyle(AppStyle.accent)
            }
            if research.busyPlans.contains(plan.id) { ProgressView("正在启用盯盘…").font(.caption) }
        }
    }
}

struct ConversationReports: View {
    let transcript: Transcript
    @ObservedObject var research: StockResearchStore
    @ObservedObject var voice: VoiceSession
    var body: some View {
        ForEach(research.reports.filter {
            transcript.role == "assistant" && $0.source.turnId == transcript.turnID && transcript.turnID != nil &&
            ($0.source.threadId == nil || voice.threadID == nil || $0.source.threadId == voice.threadID) &&
            voice.transcripts.last(where: { $0.role == "assistant" && $0.turnID == transcript.turnID })?.id == transcript.id
        }) { report in StockReportCard(report: report, research: research) }
    }
}

struct ConversationPlans: View {
    let transcript: Transcript
    @ObservedObject var model: AppModel
    @ObservedObject var research: StockResearchStore
    var body: some View {
        let reportPlanIDs = Set(research.reports.compactMap(\.planId))
        ForEach(model.plans(for: transcript).filter { !reportPlanIDs.contains($0.id) }) { plan in
            ResearchPlanCard(plan: plan, research: research,
                             isArchiving: model.archivingPlanIDs.contains(plan.id),
                             onArchive: { Task { await model.archivePlan(plan) } })
        }
    }
}

/// A row stays compact; detailed alternatives and dates live in its native sheet.
struct StockStrategyBadge: View {
    let signal: ResearchSignal?
    let onOpen: () -> Void
    private var color: Color {
        guard let signal else { return .secondary }
        let action = signal.action ?? ""
        if action.contains("卖") || action.contains("减") || signal.direction == "bearish" { return AppStyle.down }
        if action.contains("买") || action.contains("加") || signal.direction == "bullish" { return AppStyle.up }
        return AppStyle.accent
    }
    var body: some View {
        if let signal {
            Button(action: onOpen) {
                HStack(spacing: 4) {
                    if let adoption = signal.adoption {
                        Image(systemName: adoption.enabledCount > 0 ? "waveform.path.ecg" : "pause.circle")
                    }
                    Text(signal.hasStrategy ? signal.action ?? "有方案" : ResearchStyle.direction(signal.direction))
                        .fontWeight(.medium)
                    if let adoption = signal.adoption {
                        Text(adoption.label)
                    } else if let target = signal.targetPrice, signal.hasStrategy {
                        Text(AppStyle.price(target)).monospacedDigit()
                    }
                    Image(systemName: "chevron.right").font(.system(size: 8, weight: .semibold))
                }
                .font(.caption2).lineLimit(1).foregroundStyle(color)
                .padding(.horizontal, 5).padding(.vertical, 4)
                .background(color.opacity(0.07), in: RoundedRectangle(cornerRadius: 5))
                .frame(maxWidth: .infinity, minHeight: 36).contentShape(Rectangle())
            }.buttonStyle(.plain)
                .accessibilityLabel("\(signal.action ?? ResearchStyle.direction(signal.direction))，\(signal.adoption?.label ?? (signal.hasStrategy ? "尚未采用" : "分析报告"))，查看依据与方案")
        } else {
            Text("—").font(.caption).foregroundStyle(.tertiary)
                .frame(maxWidth: .infinity, minHeight: 36).accessibilityLabel("暂无 AI 分析")
        }
    }
}

struct StockRowResearchView: View {
    let code: String
    let name: String
    @ObservedObject var research: StockResearchStore
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    let reports = research.reports.filter { $0.code == code }
                    let reportPlans = Set(reports.compactMap(\.planId))
                    let plans = research.plans.filter { $0.code == code && $0.status != "archived" && !reportPlans.contains($0.id) }
                    if let error = research.error { Text(error).font(.caption).foregroundStyle(.red) }
                    ForEach(plans) { plan in ResearchPlanCard(plan: plan, research: research) }
                    ForEach(reports) { report in StockReportCard(report: report, research: research) }
                    if plans.isEmpty && reports.isEmpty && !research.isLoading {
                        ContentUnavailableView("暂无报告与策略", systemImage: "doc.text")
                    }
                    if research.nextCursors[code] != nil {
                        Button("更早的报告") { Task { await research.refresh(code: code, more: true) } }
                    }
                    if research.isLoading { ProgressView() }
                }.padding(16)
            }.background(AppStyle.canvas)
                .navigationTitle("\(name) · 报告与策略").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
                .task(id: code) { await research.refresh(code: code) }
                .refreshable { await research.refresh(code: code) }
        }.presentationDetents([.large])
    }
}

struct SidebarResearchCards: View {
    @ObservedObject var model: AppModel
    @ObservedObject var research: StockResearchStore
    @ObservedObject var voice: VoiceSession
    @State private var expanded = false
    private var reports: [StockReport] {
        let visibleTurns = Set(voice.transcripts.filter { $0.role == "assistant" }.compactMap(\.turnID))
        return Array(research.reports.filter { report in
            let sameThread = report.source.threadId == nil || report.source.threadId == voice.threadID
            let attached = sameThread && report.source.turnId.map { visibleTurns.contains($0) } == true
            return !attached && report.code == model.selectedCode
        }.prefix(5))
    }
    var body: some View {
        let reportPlanIDs = Set(research.reports.compactMap(\.planId))
        let plans = model.unattachedSidebarPlans.filter { !reportPlanIDs.contains($0.id) }
        if !reports.isEmpty || !plans.isEmpty {
            DisclosureGroup("当前股票 · 已存报告与策略", isExpanded: $expanded) {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(reports) { report in StockReportCard(report: report, research: research) }
                    ForEach(plans) { plan in
                        ResearchPlanCard(plan: plan, research: research,
                                         isArchiving: model.archivingPlanIDs.contains(plan.id),
                                         onArchive: { Task { await model.archivePlan(plan) } })
                    }
                }.padding(.top, 8)
            }.font(.caption).tint(AppStyle.accent)
        }
        if let error = research.error { Text(error).font(.caption).foregroundStyle(.red) }
    }
}

struct StockResearchHistoryView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var research: StockResearchStore
    @Environment(\.dismiss) private var dismiss
    @State private var tab = "报告"
    @State private var legacy: [LegacyStockSignal] = []
    @State private var legacyError: String?
    var body: some View {
        NavigationStack {
            VStack(spacing: 8) {
                Picker("研究记录", selection: $tab) {
                    Text("报告").tag("报告"); Text("方案").tag("方案"); Text("旧版信号").tag("旧版信号")
                }.pickerStyle(.segmented).padding(.horizontal)
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if let error = research.error { Text(error).font(.caption).foregroundStyle(.red) }
                        if tab == "报告" {
                            let values = research.reports.filter { $0.code == model.selectedCode }
                            if values.isEmpty && !research.isLoading { ContentUnavailableView("还没有分析报告", systemImage: "doc.text", description: Text("可以请助手分析当前股票并保存报告。")) }
                            ForEach(values) { report in StockReportCard(report: report, research: research) }
                            if research.nextCursors[model.selectedCode ?? "*"] != nil {
                                Button("更早的报告") { Task { await research.refresh(code: model.selectedCode, more: true) } }
                            }
                        } else if tab == "方案" {
                            ForEach(model.selectedStockPlans) { plan in
                                ResearchPlanCard(plan: plan, research: research,
                                                 isArchiving: model.archivingPlanIDs.contains(plan.id),
                                                 onArchive: { Task { await model.archivePlan(plan) } })
                            }
                            if model.selectedStockPlans.isEmpty { Text("暂无保存的方案").foregroundStyle(.secondary) }
                        } else {
                            Text("旧系统历史资料，仅供回看；不代表当前账户已启用盯盘。")
                                .font(.caption).foregroundStyle(.secondary)
                            if let legacyError { Text(legacyError).font(.caption).foregroundStyle(.secondary) }
                            ForEach(legacy) { signal in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(signal.title).font(.subheadline.weight(.medium))
                                    if let date = signal.occurredAt { Text(ResearchStyle.date(date)).font(.caption2).foregroundStyle(.secondary) }
                                    Text(signal.summary).font(.caption).textSelection(.enabled)
                                }.padding(12).frame(maxWidth: .infinity, alignment: .leading).background(.white, in: RoundedRectangle(cornerRadius: 12))
                            }
                            if legacy.isEmpty { Text("没有可用的旧版信号").font(.caption).foregroundStyle(.secondary) }
                        }
                        if research.isLoading { ProgressView() }
                    }.padding(16)
                }.refreshable { await refresh() }
            }.background(AppStyle.canvas)
                .navigationTitle("\(model.displayedStock?.name ?? model.selectedCode ?? "个股") · 研究记录")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
                .task(id: "\(model.planScopeID):\(model.selectedCode ?? "")") { await refresh() }
        }.presentationDetents([.large])
    }
    private func refresh() async {
        let code = model.selectedCode, scope = model.planScopeID
        legacy = []; legacyError = nil
        await research.refresh(code: code)
        await model.refreshPlans(code: code)
        do {
            let result = try await model.client.legacySignals(code: code)
            guard scope == model.planScopeID, code == model.selectedCode, !Task.isCancelled else { return }
            legacy = result.items; legacyError = result.warnings.first
        } catch { if scope == model.planScopeID, code == model.selectedCode { legacyError = error.localizedDescription } }
    }
}
