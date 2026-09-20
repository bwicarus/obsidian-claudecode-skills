import SwiftUI

/// Screener controls expand with their bubbles, outside the movable chart cards.
@MainActor
struct StockSelectionControls: View {
    @ObservedObject var model: StockSelectionModel
    let isScreenerActive: Bool
    let editorPresented: Bool
    let onActivate: () -> Void
    let onEdit: () -> Void
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("stocksNative.selectionExpandedCommonGroups") private var expandedCommonGroups = "[]"

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                Label("选股条件", systemImage: "line.3.horizontal.decrease")
                    .font(.subheadline.weight(.semibold))
                if model.draft.groups.count > 1 {
                    Text("组间任一满足").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                if !model.draft.disabled.isEmpty || model.draft.groups.contains(where: { !$0.enabled }) {
                    Button {
                        model.draft.disabled = []
                        for index in model.draft.groups.indices { model.draft.groups[index].enabled = true }
                        onActivate()
                        model.onContextChange?("screener", "已恢复全部条件气泡，选股结果待更新")
                    } label: { Image(systemName: "arrow.counterclockwise") }
                    .accessibilityLabel("恢复全部条件").frame(minWidth: 36, minHeight: 36)
                    .disabled(model.requiresPresetConfirmation)
                }
                if model.isEvaluating {
                    ProgressView().controlSize(.small)
                    Text("更新中").font(.caption).foregroundStyle(.secondary)
                } else if model.resultsAreCurrent, let result = model.evaluation {
                    Text("\(result.passed) 只符合").font(.caption.monospacedDigit()).foregroundStyle(AppStyle.accent)
                } else {
                    Text("点按条件即可切换").font(.caption).foregroundStyle(.secondary)
                }
                Button("编辑", systemImage: "slider.horizontal.3", action: onEdit)
                    .font(.caption).buttonStyle(.bordered).disabled(model.catalog == nil)
            }
            if model.draft.groups.isEmpty {
                Button("添加筛选条件", systemImage: "plus", action: onEdit)
                    .font(.subheadline).frame(minHeight: 44)
            } else {
                VStack(spacing: 6) {
                    ForEach(model.draft.groups) { group in conditionGroup(group) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if model.requiresPresetConfirmation {
                Text("旧方案需在编辑中核对并保存确认。").font(.caption).foregroundStyle(.orange)
            } else if let message = model.validationMessage {
                Text(message).font(.caption).foregroundStyle(.red)
            } else if model.draft.groups.contains(where: { !conditionKeys($0).isEmpty })
                        && !model.draft.groups.contains(where: { groupIsEnabled($0) }) {
                Text("全部气泡已关闭；点任一气泡即可恢复该条件。").font(.caption).foregroundStyle(.secondary)
            } else if model.resultsAreCurrent {
                Text("＋N：单独关闭此条件后，本组新增通过的股票数。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(AppStyle.canvas)
        .task(id: "\(scenePhase):\(isScreenerActive):\(editorPresented):\(model.liveEvaluationKey)") {
            guard scenePhase == .active, isScreenerActive, !editorPresented else { return }
            await model.runLive()
        }
    }

    private func conditionGroup(_ group: SelectionRuleGroup) -> some View {
        let enabled = groupIsEnabled(group)
        let common = commonConditions
        let commonAnd = Set(common.and)
        let commonNot = Set(common.not)
        let commonCount = commonAnd.count + commonNot.count
        let expanded = expandedGroupIDs.contains(group.id)
        return HStack(alignment: .top, spacing: 8) {
            Button {
                guard let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) else { return }
                let keys = Set(conditionKeys(group))
                guard !keys.isEmpty else { return }
                var disabled = Set(model.draft.disabled)
                if enabled { disabled.formUnion(keys) } else { disabled.subtract(keys) }
                model.draft.groups[index].enabled = true
                model.draft.disabled = disabled.sorted()
                onActivate()
                model.onContextChange?("screener", "条件组“\(group.name)”已\(enabled ? "停用" : "启用")，选股结果待更新")
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: enabled ? "checkmark.circle.fill" : "circle")
                    Text(group.name).lineLimit(1)
                }
                .font(.caption.weight(.semibold)).frame(width: 100, height: 44, alignment: .leading)
                .foregroundStyle(enabled ? AppStyle.accent : Color.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("条件组：\(group.name)")
            .accessibilityValue(enabled ? "已启用" : "已停用")
            SelectionBubbleFlow(spacing: 6) {
                ForEach(group.and.filter { !commonAnd.contains($0) }, id: \.self) { id in
                    conditionSwitch(id, group: group, excluded: false)
                }
                ForEach(group.not.filter { !commonNot.contains($0) }, id: \.self) { id in
                    conditionSwitch(id, group: group, excluded: true)
                }
                if commonCount > 0 && !conditionKeys(group).isEmpty {
                    Button {
                        var ids = expandedGroupIDs
                        if expanded { ids.remove(group.id) } else { ids.insert(group.id) }
                        expandedCommonGroups = String(data: (try? JSONEncoder().encode(ids.sorted())) ?? Data(), encoding: .utf8) ?? "[]"
                    } label: {
                        Label("公共 ×\(commonCount)", systemImage: expanded ? "chevron.down" : "chevron.right")
                            .font(.caption).padding(.horizontal, 10).frame(height: 44)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .accessibilityHint("只展开本组的公共条件；开关仍只影响本组")
                    if expanded {
                        ForEach(common.and, id: \.self) { id in conditionSwitch(id, group: group, excluded: false) }
                        ForEach(common.not, id: \.self) { id in conditionSwitch(id, group: group, excluded: true) }
                    }
                }
                if group.and.isEmpty && group.not.isEmpty {
                    Button("添加条件", systemImage: "plus", action: onEdit).font(.caption).frame(height: 44)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .opacity(enabled ? 1 : 0.5)
        }
        .disabled(model.catalog == nil || model.requiresPresetConfirmation)
    }

    private func conditionSwitch(_ id: String, group: SelectionRuleGroup, excluded: Bool) -> some View {
        let key = group.id + "|" + (excluded ? "not:" : "") + id
        let enabled = group.enabled && !model.draft.disabled.contains(key)
        let name = model.catalog?.criteria.first(where: { $0.id == id })?.label ?? id
        let title = (excluded ? "排除 · " : "") + name
        let effect = model.resultsAreCurrent && enabled && group.enabled
            ? model.evaluation?.groups?.first(where: { $0.id == group.id }) : nil
        let impact = excluded ? effect?.notImpacts?[id] : effect?.impacts?[id]
        return Button {
            if !group.enabled, let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) {
                model.draft.disabled = Set(model.draft.disabled).union(conditionKeys(group)).sorted()
                model.draft.groups[index].enabled = true
            }
            model.setCriterionEnabled(groupID: group.id, criterionID: id, excluded: excluded, enabled: !enabled)
            onActivate()
            model.onContextChange?("screener", "“\(title)”已\(enabled ? "停用" : "启用")，选股结果待更新")
        } label: {
            HStack(spacing: 5) {
                Image(systemName: enabled ? "checkmark.circle.fill" : "circle")
                Text(title).lineLimit(1)
                if let impact, impact > 0 {
                    Text("+\(impact)").font(.caption2.monospacedDigit().weight(.bold))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 4).padding(.vertical, 2)
                        .background(.orange.opacity(impact >= 10 ? 0.15 : 0.07), in: Capsule())
                }
            }
            .font(.caption.weight(.medium))
            .padding(.horizontal, 10).padding(.vertical, 8)
            .foregroundStyle(enabled ? (excluded ? AppStyle.up : AppStyle.accent) : Color.secondary)
            .background(enabled ? (excluded ? AppStyle.up : AppStyle.accent).opacity(0.08) : Color.white,
                        in: Capsule())
            .overlay {
                if let impact, impact > 0 {
                    Capsule().stroke(Color.orange.opacity(impact >= 10 ? 0.5 : impact >= 3 ? 0.3 : 0.15), lineWidth: 1)
                }
            }
            .opacity(impact == 0 ? 0.65 : 1)
            .frame(minHeight: 44).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue(enabled ? "已启用" : "已停用")
        .accessibilityHint(impact.map { "关闭此条件后，本组多通过 \($0) 只。点按切换并更新选股结果" }
                           ?? "点按切换并更新选股结果")
    }

    private func conditionKeys(_ group: SelectionRuleGroup) -> [String] {
        group.and.map { group.id + "|" + $0 } + group.not.map { group.id + "|not:" + $0 }
    }

    private func groupIsEnabled(_ group: SelectionRuleGroup) -> Bool {
        group.enabled && conditionKeys(group).contains { !model.draft.disabled.contains($0) }
    }

    private var expandedGroupIDs: Set<String> {
        Set((try? JSONDecoder().decode([String].self, from: Data(expandedCommonGroups.utf8))) ?? [])
    }

    private var commonConditions: (and: [String], not: [String]) {
        let groups = model.draft.groups.filter { !$0.and.isEmpty || !$0.not.isEmpty }
        guard groups.count >= 2, let first = groups.first else { return ([], []) }
        let sharedAnd = first.and.filter { id in groups.allSatisfy { $0.and.contains(id) } }
        let sharedNot = !first.not.isEmpty && groups.allSatisfy { Set($0.not) == Set(first.not) } ? first.not : []
        return (sharedAnd, sharedNot)
    }
}

/// Intrinsic-width native bubbles wrap to the available width and grow vertically.
struct SelectionBubbleFlow: Layout {
    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrangement(width: proposal.width ?? 600, subviews: subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = arrangement(width: bounds.width, subviews: subviews)
        for (index, subview) in subviews.enumerated() {
            let frame = result.frames[index]
            subview.place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                          anchor: .topLeading, proposal: ProposedViewSize(frame.size))
        }
    }

    private func arrangement(width: CGFloat, subviews: Subviews) -> (size: CGSize, frames: [CGRect]) {
        let width = max(1, width)
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var frames: [CGRect] = []
        for subview in subviews {
            let ideal = subview.sizeThatFits(.unspecified)
            let size = subview.sizeThatFits(ProposedViewSize(width: min(width, ideal.width), height: nil))
            if x > 0 && x + size.width > width {
                x = 0; y += rowHeight + spacing; rowHeight = 0
            }
            frames.append(CGRect(x: x, y: y, width: min(width, size.width), height: size.height))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return (CGSize(width: width, height: y + rowHeight), frames)
    }
}

@MainActor
struct StockSelectionSidebar: View {
    @ObservedObject var model: StockSelectionModel
    let section: StockSelectionSection
    @Binding var selectedStockCode: String?
    @Binding var editorPresented: Bool
    let onOpenStock: (String) -> Void
    let onSelectStock: (String) -> Void
    let onOverlayChange: (Bool) -> Void
    @ObservedObject var monitoring: MonitoringModel
    let onMonitoring: (String) -> Void
    @State private var groupEditor: SelectionGroupDraft?
    @State private var deletingGroup: SelectionWatchGroup?
    @State private var deletingPreset: SelectionPreset?
    @State private var addingCodes: [String]?
    @State private var choosingStocks = false
    @State private var reorderingGroups = false

    var body: some View {
        VStack(spacing: 0) {
            SelectionStatusView(model: model)
            if section == .watchlist { watchlistHeader } else { screenerHeader }
            Divider()
            results
        }
        .fullScreenCover(isPresented: $editorPresented) { SelectionRuleEditor(model: model) }
        .sheet(item: $groupEditor) { draft in
            SelectionGroupEditor(model: model, initial: draft)
        }
        .sheet(isPresented: $reorderingGroups) { SelectionGroupOrderEditor(model: model) }
        .sheet(isPresented: Binding(get: { addingCodes != nil }, set: { if !$0 { addingCodes = nil } })) {
            SelectionAddToGroupSheet(model: model, codes: addingCodes ?? [])
        }
        .alert("删除观察组？", isPresented: Binding(get: { deletingGroup != nil }, set: { if !$0 { deletingGroup = nil } })) {
            Button("取消", role: .cancel) { deletingGroup = nil }
            Button("删除", role: .destructive) {
                if let group = deletingGroup { Task { await model.mutate(operation: "group.delete", payload: SelectionMutationPayload(id: group.id)) } }
                deletingGroup = nil
            }
        } message: { Text("只删除这个组及其成员关系，其他观察组中的股票会保留。") }
        .alert("删除选股方案？", isPresented: Binding(get: { deletingPreset != nil }, set: { if !$0 { deletingPreset = nil } })) {
            Button("取消", role: .cancel) { deletingPreset = nil }
            Button("删除", role: .destructive) {
                if let preset = deletingPreset { Task { await model.mutate(operation: "preset.delete", payload: SelectionMutationPayload(id: preset.id)) } }
                deletingPreset = nil
            }
        }
        .task(id: section == .watchlist ? model.selectedGroupID : nil) {
            if section == .watchlist { await model.loadSelectedGroup() }
        }
        .onChange(of: section) { _, _ in choosingStocks = false; model.selectedCodes = [] }
        .onChange(of: editorPresented || groupEditor != nil || addingCodes != nil || reorderingGroups, initial: true) { _, visible in
            onOverlayChange(visible)
        }
        .onDisappear { onOverlayChange(false) }
    }

    private var watchlistHeader: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("观察组", selection: $model.selectedGroupID) {
                    if model.library?.groups.isEmpty != false { Text("暂无观察组").tag(Optional<String>.none) }
                    ForEach(model.library?.groups ?? []) { group in
                        Label(group.name, systemImage: group.isSmart ? "sparkles" : "folder")
                            .tag(Optional(group.id))
                    }
                }
                .labelsHidden().frame(maxWidth: .infinity, alignment: .leading)
                Menu {
                    Button("新建观察组", systemImage: "folder.badge.plus") { groupEditor = SelectionGroupDraft() }
                    Button("新建智能组", systemImage: "sparkles") { groupEditor = SelectionGroupDraft(kind: "smart") }
                    Button("调整分组顺序", systemImage: "arrow.up.arrow.down") { reorderingGroups = true }
                    if let group = model.selectedGroup {
                        Button("编辑观察组", systemImage: "slider.horizontal.3") { groupEditor = SelectionGroupDraft(group: group) }
                        Button("删除观察组", systemImage: "trash", role: .destructive) { deletingGroup = group }
                    }
                } label: { Image(systemName: "ellipsis.circle").font(.title3).frame(width: 36, height: 36) }
                .disabled(!model.canWrite)
            }
            if let group = model.selectedGroup {
                HStack {
                    Label(group.isSmart ? "智能规则自动更新" : "手动观察组", systemImage: group.isSmart ? "sparkles" : "folder")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button { Task { await model.refreshVisibleGroup(force: true) } } label: { Image(systemName: "arrow.clockwise") }
                        .disabled(model.isLoadingGroup || model.isMutating)
                }
                if let warnings = group.warnings, !warnings.isEmpty { SelectionWarnings(warnings: warnings) }
            } else {
                Text("新建一个观察组，再从市场或选股结果加入股票。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(12)
    }

    private var screenerHeader: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Picker("选股方案", selection: Binding(get: { model.selectedPresetID }, set: { model.usePreset($0) })) {
                    Text("临时方案").tag(Optional<String>.none)
                    ForEach(model.library?.presets ?? []) { preset in Text(preset.name).tag(Optional(preset.id)) }
                }
                .labelsHidden().pickerStyle(.menu).font(.subheadline).lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
                if let preset = model.selectedPreset {
                    Menu {
                        Button("编辑方案") { editorPresented = true }
                        Button("删除方案", role: .destructive) { deletingPreset = preset }
                    } label: { Image(systemName: "ellipsis.circle").font(.title3).frame(width: 36, height: 36) }
                    .disabled(!model.canWrite)
                }
                Button { Task { await model.run() } } label: {
                    if model.isEvaluating { ProgressView() } else { Label("选股", systemImage: "play.fill") }
                }
                .font(.caption).controlSize(.small).buttonStyle(.borderedProminent)
                .disabled(model.isEvaluating || model.catalog == nil || model.validationMessage != nil || model.requiresPresetConfirmation)
            }
            if model.evaluation != nil && !model.resultsAreCurrent {
                Text("条件已修改，以下仍为上次结果。").font(.caption).foregroundStyle(.orange)
            }
            if let warnings = model.selectedPreset?.warnings, !warnings.isEmpty { SelectionWarnings(warnings: warnings) }
            if model.requiresPresetConfirmation {
                Text("旧方案需要确认：请编辑条件并保存后再运行。").font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 4)
    }

    private var activeEvaluation: SelectionEvaluation? { section == .watchlist ? model.watchEvaluation : model.evaluation }
    private var activeLoading: Bool { section == .watchlist ? model.isLoadingGroup : model.isEvaluating }

    @ViewBuilder private var results: some View {
        if let result = activeEvaluation {
            VStack(spacing: 0) {
                HStack {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 6) {
                            Text("\(result.resultCount) 只股票").font(.subheadline.weight(.semibold))
                            Text(result.asOf ?? "数据时间未知").font(.caption2).foregroundStyle(.secondary)
                        }.fixedSize(horizontal: true, vertical: false)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(result.resultCount) 只股票").font(.subheadline.weight(.semibold))
                            Text(result.asOf ?? "数据时间未知").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer()
                    Menu {
                        ForEach(["code", "changePct", "price", "turnoverRate", "score"], id: \.self) { key in
                            Button(sortTitle(key)) { setSort(key, descending: key == "changePct" || key == "score") }
                        }
                        Divider()
                        Button(model.descending ? "改为升序" : "改为降序") { setSort(model.sort, descending: !model.descending) }
                    } label: { Image(systemName: "arrow.up.arrow.down").font(.caption) }
                    .disabled(activeLoading)
                    Button(choosingStocks ? "完成" : "选择") { choosingStocks.toggle(); model.selectedCodes = [] }
                        .font(.caption).disabled(section == .screener && !model.resultsAreCurrent)
                }
                .padding(.horizontal, 12).padding(.vertical, 6)
                if section == .watchlist, let quoteTime = model.watchQuoteTime {
                    Text("行情 \(quoteTime)").font(.caption2).foregroundStyle(.secondary).padding(.horizontal, 12)
                }
                if result.unfiltered == true {
                    Text("没有启用筛选条件，当前显示全部范围。").font(.caption2).foregroundStyle(.secondary).padding(.horizontal, 12)
                }
                if let unknown = result.unknown, unknown > 0, section == .screener {
                    Text("\(unknown) 只因资料不全无法判定").font(.caption2).foregroundStyle(.secondary).padding(.horizontal, 12)
                }
                if let warnings = result.warnings, !warnings.isEmpty { SelectionWarnings(warnings: warnings).padding(.horizontal, 12) }
                if choosingStocks { batchControls(result: result) }
                List {
                    ForEach(result.items) { stock in
                        MonitoringStockRow(code: stock.code, name: stock.name, sector: stock.sector,
                                           price: stock.price, changePct: stock.changePct, volumeRatio: stock.volumeRatio,
                                           turnoverRate: stock.turnoverRate, turnover: stock.turnover, marketCap: stock.marketCap,
                                           score: stock.score, summary: monitoring.library?.summary[stock.code],
                                           isSelected: choosingStocks ? model.selectedCodes.contains(stock.code) : nil,
                                           onOpen: {
                            if choosingStocks {
                                if model.selectedCodes.contains(stock.code) { model.selectedCodes.remove(stock.code) }
                                else { model.selectedCodes.insert(stock.code) }
                            } else { onSelectStock(stock.code) }
                        }, onMonitoring: { onMonitoring(stock.code) })
                        .listRowBackground(selectedStockCode == stock.code && !choosingStocks ? AppStyle.accent.opacity(0.08) : Color.clear)
                        .listRowInsets(EdgeInsets(top: 4, leading: 12, bottom: 4, trailing: 12))
                        .contextMenu {
                            Button("打开股票") { onOpenStock(stock.code) }
                            Button("盯盘规则") { onMonitoring(stock.code) }
                            Button("加入观察组") { addingCodes = [stock.code] }.disabled(!model.canWrite)
                            if section == .watchlist, let group = model.selectedGroup, !group.isSmart {
                                Button("移出这个组", role: .destructive) {
                                    Task { await model.mutate(operation: "group.remove", payload: SelectionMutationPayload(id: group.id, codes: [stock.code])) }
                                }.disabled(!model.canWrite)
                            }
                        }
                    }
                    if result.items.count < result.resultCount {
                        Button("加载更多（已显示 \(result.items.count)）") {
                            Task {
                                if section == .watchlist { await model.loadSelectedGroup(append: true) }
                                else { await model.run(append: true) }
                            }
                        }.disabled(activeLoading || (section == .screener && !model.resultsAreCurrent))
                    }
                }
                .listStyle(.plain)
                .environment(\.defaultMinListRowHeight, 44)
                .overlay { if result.items.isEmpty { ContentUnavailableView("暂无符合条件的股票", systemImage: "line.3.horizontal.decrease.circle") } }
                .refreshable {
                    if section == .watchlist { await model.refreshVisibleGroup(force: true) }
                    else { await model.run() }
                }
            }
        } else if activeLoading || model.isLoading {
            ProgressView("读取股票…").frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ContentUnavailableView(section == .watchlist ? "选择观察组" : "准备选股", systemImage: section == .watchlist ? "folder" : "line.3.horizontal.decrease.circle",
                                   description: Text(section == .watchlist ? "手动管理关注股票，或让智能规则更新成员。" : "编辑条件并运行；筛选不会改动观察池。"))
        }
    }

    private func batchControls(result: SelectionEvaluation) -> some View {
        VStack(spacing: 8) {
            HStack {
                Button("全选已显示") { model.selectedCodes = Set(result.items.map(\.code)) }
                Spacer()
                Text("已选 \(model.selectedCodes.count)").foregroundStyle(.secondary)
            }
            HStack {
                Button("加入观察组") { addingCodes = model.selectedCodes.sorted() }
                Spacer()
                if section == .watchlist, let group = model.selectedGroup, !group.isSmart {
                    Button("移出", role: .destructive) {
                        let codes = model.selectedCodes.sorted()
                        Task {
                            if await model.mutate(operation: "group.remove", payload: SelectionMutationPayload(id: group.id, codes: codes)) {
                                model.selectedCodes = []
                            }
                        }
                    }
                }
            }
            .disabled(model.selectedCodes.isEmpty || !model.canWrite)
        }
        .font(.caption).padding(12).background(AppStyle.accent.opacity(0.05))
    }

    private func sortTitle(_ key: String) -> String {
        ["code": "股票代码", "changePct": "涨跌幅", "price": "价格", "turnoverRate": "换手率", "score": "综合评分"][key] ?? key
    }

    private func setSort(_ key: String, descending: Bool) {
        model.sort = key; model.descending = descending
        Task {
            if section == .watchlist { await model.loadSelectedGroup() }
            else { await model.run() }
        }
    }
}

@MainActor
struct SelectionStatusView: View {
    @ObservedObject var model: StockSelectionModel
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            if let notice = model.notice { Text(notice).font(.caption2).foregroundStyle(.secondary) }
            if model.pendingMutation != nil {
                Button("重试确认保存结果") { Task { await model.retryPending() } }
                    .font(.caption).disabled(model.isMutating)
            } else if !model.isLibraryFresh && !model.isLoading {
                Button("同步观察池与方案") { Task { await model.refresh() } }.font(.caption)
            }
            if model.isMutating { ProgressView("正在保存…").font(.caption) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(model.error != nil || model.notice != nil || model.isMutating || !model.isLibraryFresh ? 12 : 0)
    }
}

private struct SelectionWarnings: View {
    let warnings: [String]
    var body: some View {
        ForEach(Array(warnings.prefix(3).enumerated()), id: \.offset) { _, warning in
            Text(readable(warning)).font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
    private func readable(_ warning: String) -> String {
        if warning.hasPrefix("smart_attribute_unavailable:") { return "这个智能组有尚未接入资料的规则，暂不能计算成员。" }
        if warning.hasPrefix("unknown_legacy_criterion:") {
            let name = String(warning.dropFirst("unknown_legacy_criterion:".count))
            return "旧条件“\(name)”尚未迁移；请检查现有条件，并明确保存确认后再运行。"
        }
        switch warning {
        case "feature_data_unavailable": return "部分技术与资金资料暂不可用，相关条件可能无法判定。"
        case "sector_data_unavailable": return "板块资料暂不可用。"
        case "limit_price_using_board_threshold": return "缺少精确涨跌停价时，使用所属板块的幅度规则判断。"
        case "market_unavailable", "data_unavailable": return "当前资料暂不可用，可稍后刷新。"
        case "smart_rules_empty", "invalid_rules": return "请为智能组设置有效规则。"
        default: return warning
        }
    }
}

private struct SelectionDropFrames: PreferenceKey {
    static var defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

@MainActor
private struct SelectionRuleEditor: View {
    @ObservedObject var model: StockSelectionModel
    @Environment(\.dismiss) private var dismiss
    @State private var search = ""
    @State private var selectedCriterionID: String?
    @State private var selectedTargetID: String?
    @State private var hoveredTargetID: String?
    @State private var immediateDragID: String?
    @State private var immediateDragPoint = CGPoint.zero
    @State private var editorWindowFrame = CGRect.zero
    @State private var dropFrames: [String: CGRect] = [:]
    @State private var parameterCriterionID: String?
    @State private var parametersExpanded = false
    @State private var savingPreset = false
    @State private var saveAsNew = false
    @State private var presetName = ""

    private var criteria: [SelectionCriterion] { model.catalog?.criteria ?? [] }
    private var categoryKeys: [String] {
        let present = Set(criteria.map { $0.category ?? "other" })
        let ordered = ["basic", "technical", "fund", "chips"]
        return ordered.filter { present.contains($0) } + present.subtracting(ordered).sorted()
    }
    private var cannotRun: Bool {
        model.isEvaluating || model.catalog == nil || model.validationMessage != nil || model.requiresPresetConfirmation
    }

    var body: some View {
        NavigationStack {
            GeometryReader { geometry in
                let libraryWidth = min(260, max(132, geometry.size.width * 0.27))
                let canvasWidth = max(1, geometry.size.width - libraryWidth - 1)
                HStack(spacing: 0) {
                    criterionLibrary.frame(width: libraryWidth).clipped()
                    Divider()
                    ruleCanvas(width: canvasWidth).frame(width: canvasWidth).clipped()
                }
                .frame(width: geometry.size.width, height: geometry.size.height)
                .coordinateSpace(name: "selection-editor")
                .background(NativeCriterionEditorFrame { frame in editorWindowFrame = frame })
                .onPreferenceChange(SelectionDropFrames.self) { frames in
                    dropFrames = frames
                    if immediateDragID != nil { hoveredTargetID = dropTarget(at: immediateDragPoint) }
                }
                .overlay(alignment: .topLeading) {
                    if let id = immediateDragID {
                        dragPreview(id)
                            .shadow(color: .black.opacity(0.12), radius: 8, y: 3)
                            .frame(width: min(280, geometry.size.width))
                            .position(x: min(max(140, immediateDragPoint.x), max(140, geometry.size.width - 140)),
                                      y: max(20, immediateDragPoint.y - 30))
                            .allowsHitTesting(false)
                    }
                }
                .clipped()
                .onDisappear { finishImmediateDrag(at: nil) }
            }
            .background(AppStyle.canvas)
            .safeAreaInset(edge: .bottom, spacing: 0) { actionBar }
            .navigationTitle("选股条件")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Button(model.selectedPreset == nil ? "保存方案" : "保存当前方案") {
                            presetName = model.selectedPreset?.name ?? "我的选股方案"; saveAsNew = false; savingPreset = true
                        }
                        if model.selectedPreset != nil {
                            Button("另存为新方案") { presetName = "新选股方案"; saveAsNew = true; savingPreset = true }
                        }
                        Button("恢复初始条件") {
                            if let catalog = model.catalog { model.draft = catalog.defaults }
                            selectedCriterionID = nil; selectedTargetID = nil
                        }
                    } label: { Label("方案", systemImage: "square.and.arrow.down") }
                    .disabled(!model.canWrite)
                }
            }
            .sheet(isPresented: Binding(get: { parameterCriterionID != nil }, set: { if !$0 { parameterCriterionID = nil } })) {
                parameterInspector
            }
            .alert("保存选股方案", isPresented: $savingPreset) {
                TextField("方案名称", text: $presetName)
                Button("取消", role: .cancel) { }
                Button("保存") {
                    let name = presetName.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !name.isEmpty else { return }
                    let id = saveAsNew ? nil : model.selectedPresetID
                    let definition = SelectionDefinition(groups: model.draft.groups, parameters: model.draft.parameters)
                    Task {
                        if await model.mutate(operation: "preset.save", payload: SelectionMutationPayload(id: id, name: name, definition: definition)) {
                            model.selectedPresetID = model.lastSavedPresetID ?? id
                        }
                    }
                }
            }
        }
    }

    private var criterionLibrary: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("条件库").font(.headline)
            Text("横向拖动即可放入；上下滑动浏览条件")
                .font(.caption).foregroundStyle(.secondary)
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("搜索条件", text: $search).textInputAutocapitalization(.never).autocorrectionDisabled()
                if !search.isEmpty {
                    Button { search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                        .buttonStyle(.plain).accessibilityLabel("清除条件搜索")
                }
            }
            .font(.subheadline).padding(9).background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 9))
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(categoryKeys, id: \.self) { category in
                        let items = criteria.filter { ($0.category ?? "other") == category && matchesSearch($0) }
                        if !items.isEmpty {
                            Text(categoryTitle(category)).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                                .padding(.top, 8)
                            ForEach(items) { criterion in libraryItem(criterion) }
                        }
                    }
                    if !criteria.contains(where: matchesSearch) {
                        Text(model.catalog == nil ? "正在读取条件…" : "没有匹配的条件")
                            .font(.caption).foregroundStyle(.secondary).padding(.vertical, 12)
                    }
                }
                .padding(.bottom, 12)
            }
            .scrollBounceBehavior(.basedOnSize)
            .scrollDisabled(immediateDragID != nil)
        }
        .padding(14).background(.white)
    }

    private func libraryItem(_ criterion: SelectionCriterion) -> some View {
        let selected = selectedCriterionID == criterion.id
        return HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 4) {
                Text(criterionTitle(criterion.id)).font(.subheadline).foregroundStyle(AppStyle.ink)
                    .fixedSize(horizontal: false, vertical: true)
                if let description = criterion.description, !description.isEmpty {
                    Text(description).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Spacer(minLength: 0)
            Image(systemName: selected ? "checkmark.circle.fill" : "line.3.horizontal")
                .font(.caption).foregroundStyle(selected ? AppStyle.accent : Color.secondary)
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(selected ? AppStyle.accent.opacity(0.1) : AppStyle.canvas, in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            NativeCriterionDrag(
                onTap: { selectedCriterionID = selectedCriterionID == criterion.id ? nil : criterion.id },
                onDrag: { point in updateImmediateDrag(criterion.id, at: point) },
                onEnd: { point in finishImmediateDrag(at: point) }
            )
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(criterionTitle(criterion.id))
        .accessibilityValue(selected ? "已选中，点右侧方框放入" : "可直接横向拖动或点选")
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { selectedCriterionID = selectedCriterionID == criterion.id ? nil : criterion.id }
    }

    private func ruleCanvas(width: CGFloat) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                SelectionStatusView(model: model)
                if let message = model.validationMessage { Text(message).font(.caption).foregroundStyle(.red) }
                if model.requiresPresetConfirmation {
                    Text("旧方案含尚未迁移的条件。请核对保留的条件，并从“方案”保存确认后再运行。")
                        .font(.subheadline).foregroundStyle(.orange)
                    SelectionWarnings(warnings: model.selectedPreset?.warnings ?? [])
                }
                HStack {
                    Text("条件组").font(.headline)
                    Spacer()
                    Text("组内全部满足 · 组间任一满足").font(.caption).foregroundStyle(.secondary)
                }
                DisclosureGroup("筛选参数 · 整个方案共用", isExpanded: $parametersExpanded) {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 240), spacing: 16)], spacing: 10) {
                        ForEach(model.catalog?.parameters ?? []) { parameter in parameterField(parameter) }
                    }.padding(.top, 10)
                }
                .font(.subheadline).padding(12).background(.white, in: RoundedRectangle(cornerRadius: 12))
                ForEach(Array(model.draft.groups.enumerated()), id: \.element.id) { index, group in
                    if index > 0 {
                        HStack { Rectangle().frame(height: 1); Text("或者 · OR").fixedSize(); Rectangle().frame(height: 1) }
                            .font(.caption.weight(.medium)).foregroundStyle(.secondary.opacity(0.65))
                    }
                    ruleGroup(group, width: max(1, width - 36))
                }
                Button {
                    let group = SelectionRuleGroup(name: "条件组 \(model.draft.groups.count + 1)")
                    model.draft.groups.append(group)
                    selectedTargetID = group.id + "|and"
                    model.onContextChange?("screener", "新增选股条件组，选股结果待更新")
                } label: {
                    HStack { Image(systemName: "plus.circle"); Text("添加条件组（OR）"); Spacer() }
                        .font(.subheadline).padding(14).frame(maxWidth: .infinity)
                        .background(.white, in: RoundedRectangle(cornerRadius: 12))
                }.buttonStyle(.plain)
                if model.draft.groups.isEmpty {
                    Text("先添加一个条件组，再把左侧条件拖入方框。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if model.resultsAreCurrent, let history = model.evaluation?.history {
                    SelectionHistoryView(history: history)
                }
            }
            .padding(18)
        }
        .scrollDismissesKeyboard(.interactively)
    }

    private func ruleGroup(_ group: SelectionRuleGroup, width: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                TextField("条件组名称", text: Binding(get: { model.draft.groups.first { $0.id == group.id }?.name ?? "" }, set: { value in
                    if let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) { model.draft.groups[index].name = value }
                })).font(.headline)
                Toggle("启用条件组", isOn: Binding(get: { model.draft.groups.first { $0.id == group.id }?.enabled ?? false }, set: { value in
                    if let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) { model.draft.groups[index].enabled = value }
                })).labelsHidden().fixedSize()
                Button(role: .destructive) { model.removeRuleGroup(group.id) } label: { Image(systemName: "trash") }
                    .buttonStyle(.plain).accessibilityLabel("删除条件组 \(group.name)")
            }
            if width >= 548 {
                HStack(alignment: .top, spacing: 10) {
                    conditionZone(group, excluded: false).frame(width: (width - 38) / 2)
                    conditionZone(group, excluded: true).frame(width: (width - 38) / 2)
                }
            } else {
                VStack(spacing: 10) {
                    conditionZone(group, excluded: false)
                    conditionZone(group, excluded: true)
                }
            }
            if group.and.isEmpty && group.not.isEmpty {
                    Text("空组不参与筛选；所有组均无条件时返回全市场。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if model.resultsAreCurrent, let effect = model.evaluation?.groups?.first(where: { $0.id == group.id }) {
                Text("本组通过 \(effect.baselinePassed ?? 0) 只 · 资料不全 \(effect.unknown ?? 0) 只")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(14).background(.white, in: RoundedRectangle(cornerRadius: 16))
    }

    private func conditionZone(_ group: SelectionRuleGroup, excluded: Bool) -> some View {
        let zoneID = group.id + (excluded ? "|not" : "|and")
        let highlighted = hoveredTargetID == zoneID || selectedTargetID == zoneID
        let tint = excluded ? AppStyle.up : AppStyle.accent
        let ids = excluded ? group.not : group.and
        return VStack(alignment: .leading, spacing: 10) {
            Button { placeSelected(in: group.id, excluded: excluded) } label: {
                HStack {
                    Text(excluded ? "排除条件" : "满足条件").font(.subheadline.weight(.semibold))
                    Spacer(minLength: 4)
                    Text(excluded ? "NOT · 全部不满足" : "AND · 全部满足").font(.caption2)
                }
                .foregroundStyle(tint).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain)
            if ids.isEmpty {
                Button { placeSelected(in: group.id, excluded: excluded) } label: {
                    Text(selectedCriterionID == nil ? "将左侧条件拖到这里" : "点此放入选中的条件")
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, minHeight: 50, alignment: .center)
                        .contentShape(Rectangle())
                }.buttonStyle(.plain)
            } else {
                SelectionBubbleFlow(spacing: 7) {
                    ForEach(ids, id: \.self) { id in criterionChip(id, group: group, excluded: excluded) }
                }
                if selectedCriterionID != nil {
                    Button("放入选中条件", systemImage: "plus") { placeSelected(in: group.id, excluded: excluded) }
                        .font(.caption).buttonStyle(.plain)
                }
            }
        }
        .padding(12).frame(maxWidth: .infinity, minHeight: 104, alignment: .topLeading)
        .background {
            RoundedRectangle(cornerRadius: 12)
                .fill(tint.opacity(highlighted ? 0.1 : 0.035))
                .onTapGesture { placeSelected(in: group.id, excluded: excluded) }
        }
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(tint.opacity(highlighted ? 0.7 : 0.25), style: StrokeStyle(lineWidth: highlighted ? 2 : 1, dash: highlighted ? [] : [5, 4]))
                .allowsHitTesting(false)
        }
        .background {
            GeometryReader { geometry in
                Color.clear.preference(key: SelectionDropFrames.self,
                    value: [zoneID: geometry.frame(in: .named("selection-editor"))])
            }
        }
        .dropDestination(for: String.self) { items, _ in
            acceptDrop(items, groupID: group.id, excluded: excluded)
        } isTargeted: { targeted in
            if targeted { hoveredTargetID = zoneID }
            else if hoveredTargetID == zoneID { hoveredTargetID = nil }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(group.name)，\(excluded ? "排除条件" : "满足条件")放置区")
    }

    private func criterionChip(_ id: String, group: SelectionRuleGroup, excluded: Bool) -> some View {
        let key = group.id + "|" + (excluded ? "not:" : "") + id
        let enabled = !model.draft.disabled.contains(key)
        let tint = excluded ? AppStyle.up : AppStyle.accent
        let hasParameters = !(criteria.first { $0.id == id }?.parameters ?? []).isEmpty
        let effect = model.resultsAreCurrent ? model.evaluation?.groups?.first { $0.id == group.id } : nil
        let impact = excluded ? effect?.notImpacts?[id] : effect?.impacts?[id]
        return HStack(spacing: 5) {
            Button {
                model.setCriterionEnabled(groupID: group.id, criterionID: id, excluded: excluded, enabled: !enabled)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: enabled ? "checkmark.circle.fill" : "circle")
                    Text(criterionTitle(id)).fixedSize(horizontal: false, vertical: true)
                    if let impact, impact > 0 { Text("+\(impact)").foregroundStyle(.orange).monospacedDigit() }
                }.contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityValue(enabled && group.enabled ? "已启用" : "已停用")
            if hasParameters {
                Button { parameterCriterionID = id } label: { Image(systemName: "slider.horizontal.3") }
                    .buttonStyle(.plain).accessibilityLabel("调整\(criterionTitle(id))的参数")
            }
            Button { model.setCriterion(groupID: group.id, criterionID: id, excluded: excluded, present: false) } label: {
                Image(systemName: "xmark").font(.caption2)
            }.buttonStyle(.plain).accessibilityLabel("移除\(criterionTitle(id))")
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(enabled && group.enabled ? tint : Color.secondary)
        .padding(.horizontal, 9).padding(.vertical, 8)
        .background(enabled && group.enabled ? tint.opacity(0.1) : Color.white, in: RoundedRectangle(cornerRadius: 9))
        .draggable(SelectionCriterionTransfer(criterionID: id, sourceGroupID: group.id, sourceExcluded: excluded).stringValue) {
            dragPreview(id)
        }
    }

    private func dragPreview(_ id: String) -> some View {
        Text(criterionTitle(id)).font(.subheadline.weight(.medium))
            .padding(.horizontal, 12).padding(.vertical, 9)
            .foregroundStyle(AppStyle.accent).background(.white, in: RoundedRectangle(cornerRadius: 10))
    }

    private func updateImmediateDrag(_ id: String, at windowPoint: CGPoint) {
        guard editorWindowFrame.width > 0, editorWindowFrame.height > 0 else { return }
        let local = CGPoint(x: windowPoint.x - editorWindowFrame.minX, y: windowPoint.y - editorWindowFrame.minY)
        immediateDragID = id
        immediateDragPoint = local
        hoveredTargetID = dropTarget(at: local)
    }

    private func dropTarget(at point: CGPoint) -> String? {
        guard CGRect(origin: .zero, size: editorWindowFrame.size).contains(point) else { return nil }
        return dropFrames.first { $0.value.contains(point) }?.key
    }

    private func finishImmediateDrag(at windowPoint: CGPoint?) {
        defer { immediateDragID = nil; hoveredTargetID = nil }
        guard let id = immediateDragID, let windowPoint else { return }
        let local = CGPoint(x: windowPoint.x - editorWindowFrame.minX, y: windowPoint.y - editorWindowFrame.minY)
        guard let target = dropTarget(at: local) else { return }
        for group in model.draft.groups {
            for excluded in [false, true] where target == group.id + (excluded ? "|not" : "|and") {
                _ = acceptDrop([SelectionCriterionTransfer(criterionID: id).stringValue], groupID: group.id, excluded: excluded)
                return
            }
        }
    }

    private func placeSelected(in groupID: String, excluded: Bool) {
        selectedTargetID = groupID + (excluded ? "|not" : "|and")
        guard let id = selectedCriterionID else { return }
        _ = acceptDrop([SelectionCriterionTransfer(criterionID: id).stringValue], groupID: groupID, excluded: excluded)
    }

    private func acceptDrop(_ items: [String], groupID: String, excluded: Bool) -> Bool {
        guard !items.isEmpty, model.catalog != nil else { return false }
        let transfers = items.compactMap(SelectionCriterionTransfer.decode)
        guard transfers.count == items.count else { return false }
        var next = model.draft
        let allowedIDs = Set(criteria.map(\.id))
        for transfer in transfers {
            guard next.applyCriterionDrop(transfer, targetGroupID: groupID, excluded: excluded, allowedIDs: allowedIDs) else { return false }
        }
        if next != model.draft {
            model.draft = next
            model.onContextChange?("screener", "选股条件已拖入\(excluded ? "排除" : "满足")方框，选股结果待更新")
        }
        selectedCriterionID = nil
        selectedTargetID = groupID + (excluded ? "|not" : "|and")
        hoveredTargetID = nil
        return true
    }

    private func matchesSearch(_ criterion: SelectionCriterion) -> Bool {
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        return query.isEmpty || criterion.label.localizedCaseInsensitiveContains(query)
            || categoryTitle(criterion.category ?? "other").localizedCaseInsensitiveContains(query)
            || criterion.id.localizedCaseInsensitiveContains(query)
    }

    private func categoryTitle(_ key: String) -> String {
        ["basic": "基础行情", "technical": "技术走势", "fund": "资金动向", "chips": "筹码分布"][key] ?? "其他条件"
    }

    private func parameterValue(_ id: String) -> Double {
        model.draft.parameters[id] ?? model.catalog?.parameters.first { $0.id == id }?.defaultValue ?? 0
    }

    private func criterionTitle(_ id: String) -> String {
        func value(_ key: String) -> String { parameterValue(key).formatted(.number.precision(.fractionLength(0...2))) }
        switch id {
        case "price_below_limit": return "股价 ≤ \(value("max_price")) 元"
        case "price_above_min": return "股价 ≥ \(value("min_price")) 元"
        case "turnover_in_range": return "换手率 \(value("turnover_rate_min"))–\(value("turnover_rate_max"))%"
        case "profit_ratio_above": return "筹码获利 ≥ \(value("profit_ratio_above_value"))%"
        case "profit_ratio_below": return "筹码获利 ≤ \(value("profit_ratio_below_value"))%"
        case "chip_concentration_below": return "筹码集中度 ≤ \(value("chip_concentration_max_value"))%"
        case "kdj_recent_cross": return "近 \(value("kdj_cross_days")) 日 KDJ 金叉"
        default: return criteria.first { $0.id == id }?.label ?? id
        }
    }

    private func parameterField(_ parameter: SelectionParameter) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(parameter.label).font(.caption)
                if let minimum = parameter.minimum, let maximum = parameter.maximum {
                    Text("\(minimum.formatted()) – \(maximum.formatted())").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            TextField(parameter.label, value: Binding(get: { parameterValue(parameter.id) }, set: { value in
                if value.isFinite { model.draft.parameters[parameter.id] = value }
            }), format: .number)
                .keyboardType(.numbersAndPunctuation).multilineTextAlignment(.trailing)
                .textFieldStyle(.roundedBorder).frame(width: 100).font(.caption.monospacedDigit())
        }
    }

    private var parameterInspector: some View {
        NavigationStack {
            Form {
                Text("参数在整个方案中共用；修改后，同一条件的所有气泡一起更新。")
                    .font(.caption).foregroundStyle(.secondary)
                if let id = parameterCriterionID, let criterion = criteria.first(where: { $0.id == id }) {
                    if let description = criterion.description { Text(description).font(.subheadline) }
                    ForEach(criterion.parameters ?? [], id: \.self) { parameterID in
                        if let parameter = model.catalog?.parameters.first(where: { $0.id == parameterID }) { parameterField(parameter) }
                    }
                }
                if let message = model.validationMessage { Text(message).font(.caption).foregroundStyle(.red) }
            }
            .navigationTitle("条件参数").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { parameterCriterionID = nil } } }
        }
        .presentationDetents([.medium, .large])
    }

    private var actionBar: some View {
        HStack(spacing: 14) {
            Button { Task { await model.run(includeHistory: true) } } label: {
                Label("近期效果", systemImage: "clock.arrow.circlepath")
            }.font(.subheadline).disabled(cannotRun)
            Spacer(minLength: 4)
            if model.resultsAreCurrent, let result = model.evaluation {
                Text("\(result.passed) / \(result.total) 只符合").font(.caption).foregroundStyle(.secondary)
            }
            Button {
                Task { await model.run(); if model.resultsAreCurrent { dismiss() } }
            } label: {
                HStack(spacing: 6) { if model.isEvaluating { ProgressView() }; Text("运行选股") }
                    .padding(.horizontal, 10).padding(.vertical, 4)
            }.buttonStyle(.borderedProminent).disabled(cannotRun)
        }
        .padding(.horizontal, 18).padding(.vertical, 10).background(.white)
        .overlay(alignment: .top) { Divider() }
    }
}

private struct SelectionHistoryView: View {
    let history: SelectionHistory
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("近期历史效果 · \(history.days) 个交易日").font(.headline)
            Text("按当日资料筛选，比较下一交易日收盘表现与全市场等权平均。无下一日价格的样本不计入。").font(.caption).foregroundStyle(.secondary)
            if history.status == "insufficient_history" {
                Text("历史资料不足，暂不能计算效果。").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(history.groups) { group in
                VStack(alignment: .leading, spacing: 6) {
                    Text(group.name).font(.subheadline.weight(.medium))
                    Text("\(group.sampleDays) 个交易日 · \(group.hits) 次命中 · 日均 \(group.averageHits.map { $0.formatted(.number.precision(.fractionLength(0...1))) } ?? "—") 只")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Text("平均超额 \(group.meanExcessPct.map { String(format: "%+.2f%%", $0) } ?? "—")")
                        Spacer()
                        Text("相关系数 \(group.ic.map { String(format: "%.3f", $0) } ?? "—")")
                    }.font(.caption.monospacedDigit())
                }
            }
        }
        .padding(16).background(.white, in: RoundedRectangle(cornerRadius: 16))
    }
}

private struct SelectionGroupDraft: Identifiable {
    var id: String = UUID().uuidString
    var existingID: String?
    var name = ""
    var kind = "manual"
    var rules = SelectionSmartRules()
    var settings: [String: SelectionJSONValue]?
    init(kind: String = "manual") { self.kind = kind }
    init(group: SelectionWatchGroup) {
        existingID = group.id; name = group.name; kind = group.kind
        rules = group.rules ?? SelectionSmartRules(); settings = group.settings
    }
    var realtimeEnabled: Bool {
        get { if case .bool(let value)? = settings?["realtimeEnabled"] { return value }; return true }
        set { if settings == nil { settings = [:] }; settings?["realtimeEnabled"] = .bool(newValue) }
    }
    var realtimeInterval: Int {
        get { if case .number(let value)? = settings?["realtimeIntervalSec"] { return Int(value) }; return 5 }
        set { if settings == nil { settings = [:] }; settings?["realtimeIntervalSec"] = .number(Double(newValue)) }
    }
    var refreshInterval: Int {
        get { if case .number(let value)? = settings?["refreshIntervalSec"] { return Int(value) }; return 60 }
        set { if settings == nil { settings = [:] }; settings?["refreshIntervalSec"] = .number(Double(newValue)) }
    }
}

@MainActor
private struct SelectionGroupEditor: View {
    @ObservedObject var model: StockSelectionModel
    @Environment(\.dismiss) private var dismiss
    @State private var draft: SelectionGroupDraft
    @State private var usesDefinition: Bool
    @State private var definitionSource: String
    init(model: StockSelectionModel, initial: SelectionGroupDraft) {
        self.model = model; _draft = State(initialValue: initial)
        _usesDefinition = State(initialValue: initial.rules.definition != nil)
        _definitionSource = State(initialValue: initial.rules.definition == nil ? "draft" : "saved")
    }
    var body: some View {
        NavigationStack {
            Form {
                Section { SelectionStatusView(model: model) }
                Section("观察组") {
                    TextField("名称", text: $draft.name)
                    LabeledContent("类型", value: draft.kind == "smart" ? "智能规则组" : "手动观察组")
                }
                Section("更新设置") {
                    Toggle("自动更新行情", isOn: $draft.realtimeEnabled)
                    if draft.realtimeEnabled { Stepper("行情每 \(draft.realtimeInterval) 秒刷新", value: $draft.realtimeInterval, in: 3...60) }
                    Stepper("成员资料每 \(draft.refreshInterval) 秒刷新", value: $draft.refreshInterval, in: 10...3600, step: 10)
                }
                if draft.kind == "smart" {
                    Section("自动加入规则") {
                        Picker("匹配方式", selection: $draft.rules.match) {
                            Text("满足全部规则").tag("all")
                            Text("满足任意规则").tag("any")
                        }
                        ForEach(model.catalog?.smartAttributes ?? []) { attribute in
                            Toggle(attribute.label, isOn: Binding(get: { draft.rules.attrs.contains(attribute.id) }, set: { selected in
                                draft.rules.attrs.removeAll { $0 == attribute.id }
                                if selected { draft.rules.attrs.append(attribute.id) }
                            }))
                            .disabled(attribute.status != nil && attribute.status != "available" && !draft.rules.attrs.contains(attribute.id))
                        }
                        Stepper("最多 \(draft.rules.limit) 只", value: $draft.rules.limit, in: 1...200)
                    }
                    Section {
                        Toggle("同时应用选股条件", isOn: $usesDefinition)
                        if usesDefinition {
                            Picker("选股条件来源", selection: $definitionSource) {
                                if draft.existingID != nil { Text("当前保存的条件").tag("saved") }
                                Text("当前编辑的选股条件").tag("draft")
                                ForEach(model.library?.presets ?? []) { preset in Text(preset.name).tag(preset.id) }
                            }
                            .onChange(of: definitionSource) { _, id in
                                if id == "draft" { draft.rules.definition = model.draft }
                                else if id != "saved" { draft.rules.definition = model.library?.presets.first { $0.id == id }?.definition }
                            }
                            Text("保存后使用这份条件快照；修改其他方案不会悄悄改变这个组。").font(.caption).foregroundStyle(.secondary)
                        }
                    } footer: { Text("智能组在打开观察池时定期更新，也可手动刷新；资料暂缺时会显示说明。") }
                }
            }
            .navigationTitle(draft.existingID == nil ? "新建观察组" : "编辑观察组")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        var rules = draft.rules
                        rules.definition = usesDefinition ? (rules.definition ?? model.draft) : nil
                        let payload = SelectionMutationPayload(id: draft.existingID,
                                                               name: draft.name.trimmingCharacters(in: .whitespacesAndNewlines),
                                                               kind: draft.kind, rules: draft.kind == "smart" ? rules : nil,
                                                               settings: draft.settings)
                        Task {
                            if await model.mutate(operation: draft.existingID == nil ? "group.create" : "group.update", payload: payload) {
                                model.selectedGroupID = model.lastSavedGroupID ?? draft.existingID ?? model.selectedGroupID
                                dismiss()
                            }
                        }
                    }
                    .disabled(draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.canWrite)
                }
            }
        }
    }
}

@MainActor
struct SelectionAddToGroupSheet: View {
    @ObservedObject var model: StockSelectionModel
    let codes: [String]
    @Environment(\.dismiss) private var dismiss
    @State private var newName = ""
    @State private var selectedGroups: Set<String> = []
    var body: some View {
        NavigationStack {
            List {
                Section { SelectionStatusView(model: model) }
                Section("将 \(codes.count) 只股票加入") {
                    ForEach(model.manualGroups) { group in
                        Toggle(isOn: Binding(get: { selectedGroups.contains(group.id) }, set: { selected in
                            if selected { selectedGroups.insert(group.id) } else { selectedGroups.remove(group.id) }
                        })) { Label(group.name, systemImage: "folder") }
                    }
                    Button("加入选中的 \(selectedGroups.count) 个组") {
                        Task {
                            if await model.mutate(operation: "group.add", payload: SelectionMutationPayload(groupIds: selectedGroups.sorted(), codes: codes)) { dismiss() }
                        }
                    }.disabled(selectedGroups.isEmpty || !model.canWrite)
                }
                Section("新建观察组并加入") {
                    TextField("观察组名称", text: $newName)
                    Button("新建并加入") {
                        let name = newName.trimmingCharacters(in: .whitespacesAndNewlines)
                        Task {
                            if await model.mutate(operation: "group.create", payload: SelectionMutationPayload(name: name, kind: "manual", codes: codes)) { dismiss() }
                        }
                    }.disabled(newName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !model.canWrite)
                }
            }
            .navigationTitle("加入观察组")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } } }
        }
    }
}

@MainActor
private struct SelectionGroupOrderEditor: View {
    @ObservedObject var model: StockSelectionModel
    @Environment(\.dismiss) private var dismiss
    @State private var ids: [String]
    init(model: StockSelectionModel) {
        self.model = model; _ids = State(initialValue: model.library?.groups.map(\.id) ?? [])
    }
    var body: some View {
        NavigationStack {
            List {
                Section { SelectionStatusView(model: model) }
                Section("拖动右侧手柄调整顺序") {
                    ForEach(ids, id: \.self) { id in
                        if let group = model.library?.groups.first(where: { $0.id == id }) {
                            Label(group.name, systemImage: group.isSmart ? "sparkles" : "folder")
                        }
                    }
                    .onMove { source, destination in ids.move(fromOffsets: source, toOffset: destination) }
                }
            }
            .environment(\.editMode, .constant(.active))
            .navigationTitle("观察组顺序")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        guard Set(ids) == Set(model.library?.groups.map(\.id) ?? []) else {
                            ids = model.library?.groups.map(\.id) ?? []
                            model.error = "观察组已变化，已载入最新列表，请重新排列。"
                            return
                        }
                        Task { if await model.mutate(operation: "group.reorder", payload: SelectionMutationPayload(ids: ids)) { dismiss() } }
                    }.disabled(!model.canWrite)
                }
            }
        }
    }
}
