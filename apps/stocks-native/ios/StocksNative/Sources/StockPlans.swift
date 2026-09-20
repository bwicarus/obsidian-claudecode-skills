import SwiftUI

struct StockPlan: Codable, Identifiable, Equatable {
    let id: String
    let schemaVersion: Int
    let revision: Int
    let code: String
    let title: String
    let summary: String
    let mode: String
    let status: String
    let recommendedVariantId: String
    let variants: [StockPlanVariant]
    let basis: StockPlanBasis
    let source: StockPlanSource
    let createdAt: String
    let updatedAt: String
}

struct StockPlanVariant: Codable, Identifiable, Equatable {
    let id: String
    let label: String
    let action: String
    let targetPrice: Double?
    let targetKind: String
    let suggestedShares: Int?
    let urgency: String
    let reason: String
    let rules: [StockPlanRule]
}

struct StockPlanRule: Codable, Equatable {
    let type: String
    let value: Double?

    var display: String {
        let title: String
        let unit: String
        switch type {
        case "hard_stop": title = "止损"; unit = "元"
        case "take_profit": title = "止盈"; unit = "元"
        case "add_price": title = "加仓价"; unit = "元"
        case "target_buy": title = "买点"; unit = "元"
        case "pct_stop": title = "浮亏止损"; unit = "%"
        case "pct_take": title = "浮盈止盈"; unit = "%"
        case "trailing_drawdown": title = "峰值回撤"; unit = "%"
        case "max_shares": title = "最多"; unit = "股"
        case "no_add": return "不再加仓"
        default: return type
        }
        guard let value else { return title }
        return "\(title) \(value.formatted(.number.precision(.fractionLength(0...2))))\(unit)"
    }
}

struct StockPlanBasis: Codable, Equatable {
    let marketAsOf: String
    let referencePrice: Double?
    let contextRevision: Int?
}

struct StockPlanSource: Codable, Equatable {
    let sessionId: String?
    let threadId: String?
    let turnId: String?
    let messageId: String?
    let requestId: String?
}

struct StockPlanListResponse: Codable {
    let revision: Int
    let items: [StockPlan]
    let asOf: String
}

struct StockPlanResponse: Codable {
    let revision: Int
    let plan: StockPlan
}

struct StockPlanMutationResponse: Codable {
    let success: Bool
    let requestId: String
    let revision: Int
    let operation: String
    let planId: String
    let plan: StockPlan
    let replayed: Bool
}

struct StockPlanArchiveRequest: Codable {
    let id: String
    let requestId: String
    let expectedRevision: Int
}

struct StockPlanAPIError: LocalizedError {
    let status: Int
    let code: String?
    let message: String
    var errorDescription: String? { message }
}

/// One persisted plan is rendered by both the conversation and stock detail.
/// Saved suggestions and verified monitoring receipts have distinct states.
struct StockPlanCard: View {
    let plan: StockPlan
    var isArchiving = false
    var onArchive: (() -> Void)? = nil
    var onChoose: ((StockPlanVariant) -> Void)? = nil
    var activationBusy = false
    var adoptedVariantID: String? = nil
    var onOpenStock: ((String) -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(plan.title).font(.subheadline.weight(.semibold))
                Spacer(minLength: 0)
                if let onOpenStock {
                    Button { onOpenStock(plan.code) } label: {
                        HStack(spacing: 3) {
                            Text(plan.code).font(.caption.monospacedDigit())
                            Image(systemName: "chevron.right").font(.system(size: 8, weight: .semibold))
                        }.padding(.vertical, 4).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain).foregroundStyle(AppStyle.accent)
                    .fixedSize().accessibilityLabel("打开 \(plan.code) 个股详情")
                } else {
                    Text(plan.code).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                if isArchiving {
                    ProgressView().controlSize(.mini)
                } else if plan.status != "archived", let onArchive {
                    Menu {
                        Button("归档方案", systemImage: "archivebox", action: onArchive)
                    } label: { Image(systemName: "ellipsis").frame(minWidth: 28, minHeight: 28) }
                        .buttonStyle(.plain).accessibilityLabel("方案操作")
                }
            }
            HStack(spacing: 6) {
                Text(plan.status == "archived" ? "已归档" : "已保存")
                Text("·")
                Text("行情 \(ResearchStyle.date(plan.basis.marketAsOf))")
            }
            .font(.caption2).foregroundStyle(.secondary)
            Text("生成于 \(ResearchStyle.date(plan.createdAt))")
                .font(.caption2).foregroundStyle(.tertiary)
            if !plan.summary.isEmpty {
                Text(plan.summary).font(.caption).foregroundStyle(AppStyle.ink)
                    .fixedSize(horizontal: false, vertical: true)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 180), spacing: 8)], alignment: .leading, spacing: 8) {
                ForEach(plan.variants) { variant in
                    VStack(spacing: 6) {
                        StockPlanVariantCard(variant: variant, recommended: variant.id == plan.recommendedVariantId)
                        if let onChoose, plan.status != "archived" {
                            Button(adoptedVariantID == variant.id ? "已采用" : "按此方案盯盘") { onChoose(variant) }
                                .font(.caption).buttonStyle(.bordered).tint(AppStyle.accent)
                                .disabled(activationBusy || adoptedVariantID != nil || (variant.rules.isEmpty && variant.targetPrice == nil))
                        }
                    }
                }
            }
        }
        .foregroundStyle(AppStyle.ink)
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 14))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(plan.code) 的操作方案：\(plan.title)")
    }
}

private struct StockPlanVariantCard: View {
    let variant: StockPlanVariant
    let recommended: Bool
    @State private var reasonExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Text(variant.label).font(.caption.weight(.semibold))
                if recommended {
                    Text("推荐").font(.caption2.weight(.medium)).foregroundStyle(AppStyle.accent)
                }
                Spacer(minLength: 0)
                Text(variant.action).font(.caption.weight(.medium))
            }
            if let target = variant.targetPrice {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text(variant.targetKind).font(.caption2).foregroundStyle(.secondary)
                    Text(target.formatted(.number.precision(.fractionLength(2))))
                        .font(.subheadline.weight(.semibold)).monospacedDigit()
                    Text("元").font(.caption2).foregroundStyle(.secondary)
                }
            }
            if let shares = variant.suggestedShares {
                Text("建议 \(shares.formatted()) 股").font(.caption2).foregroundStyle(.secondary)
            }
            if !variant.rules.isEmpty {
                Text(variant.rules.map(\.display).joined(separator: " · "))
                    .font(.caption2).foregroundStyle(AppStyle.accent)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(variant.reason).font(.caption).foregroundStyle(.secondary)
                .lineLimit(variant.reason.count > 50 && !reasonExpanded ? 3 : nil)
                .fixedSize(horizontal: false, vertical: true)
            if variant.reason.count > 50 {
                Button(reasonExpanded ? "收起依据" : "展开依据") { reasonExpanded.toggle() }
                    .font(.caption2).buttonStyle(.plain).foregroundStyle(AppStyle.accent)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(.white, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(recommended ? AppStyle.accent.opacity(0.35) : .clear))
    }
}

/// The caller supplies only the selected stock's plans and owns the outer scroll.
struct StockPlanDetailPanel: View {
    let plans: [StockPlan]
    var isLoading = false
    var error: String? = nil
    var archivingIDs: Set<String> = []
    var onArchive: ((StockPlan) -> Void)? = nil
    var onOpenStock: ((String) -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("操作方案").font(.headline)
                Spacer()
                if isLoading { ProgressView().controlSize(.small) }
            }
            if let error {
                Text(error).font(.caption).foregroundStyle(.secondary)
            }
            if plans.isEmpty && !isLoading && error == nil {
                Text("分析后保存的方案会显示在这里，也会保留在 AI 对话中。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(plans) { plan in
                StockPlanCard(plan: plan, isArchiving: archivingIDs.contains(plan.id),
                              onArchive: onArchive.map { action in { action(plan) } },
                              onOpenStock: onOpenStock)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct StockPlanHistoryView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var includesArchived = false

    private var plans: [StockPlan] {
        model.selectedStockPlans.filter { includesArchived || $0.status != "archived" }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                StockPlanDetailPanel(plans: plans, isLoading: model.isLoadingPlans, error: model.planError,
                                     archivingIDs: model.archivingPlanIDs,
                                     onArchive: { plan in Task { await model.archivePlan(plan) } },
                                     onOpenStock: { code in dismiss(); model.openStock(code) })
                .padding(20)
            }
            .background(AppStyle.canvas)
            .refreshable { await model.refreshPlans(code: model.selectedCode) }
            .navigationTitle("\(model.displayedStock?.name ?? model.selectedCode ?? "个股") · 方案")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Toggle(isOn: $includesArchived) { Label("包括已归档", systemImage: "archivebox") }
                        .toggleStyle(.button).font(.caption)
                }
                ToolbarItem(placement: .topBarTrailing) { Button("完成") { dismiss() } }
            }
            .task(id: "\(model.planScopeID):\(model.selectedCode ?? "")") {
                await model.refreshPlans(code: model.selectedCode)
            }
        }
        .presentationDetents([.large])
    }
}
