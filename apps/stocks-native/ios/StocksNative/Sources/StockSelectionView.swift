import SwiftUI

/// Persistent controls belong to the workspace, outside the movable chart cards.
@MainActor
struct StockSelectionControls: View {
    @ObservedObject var model: StockSelectionModel
    let editorPresented: Bool
    let onEdit: () -> Void
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                Label("选股条件", systemImage: "line.3.horizontal.decrease")
                    .font(.subheadline.weight(.semibold))
                if model.draft.groups.count > 1 {
                    Text("组间任一满足").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
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
                ScrollView(.vertical) {
                    VStack(spacing: 2) {
                        ForEach(model.draft.groups) { group in conditionGroup(group) }
                    }
                }
                .frame(height: CGFloat(min(model.draft.groups.count, 3)) * 46)
            }
            if model.requiresPresetConfirmation {
                Text("旧方案需在编辑中核对并保存确认。").font(.caption).foregroundStyle(.orange)
            } else if let message = model.validationMessage {
                Text(message).font(.caption).foregroundStyle(.red)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(AppStyle.canvas)
        .task(id: "\(scenePhase):\(editorPresented):\(model.liveEvaluationKey)") {
            guard scenePhase == .active, !editorPresented else { return }
            await model.runLive()
        }
    }

    private func conditionGroup(_ group: SelectionRuleGroup) -> some View {
        HStack(spacing: 8) {
            Button {
                guard let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) else { return }
                model.draft.groups[index].enabled.toggle()
                model.onContextChange?("screener", "条件组“\(group.name)”已\(group.enabled ? "停用" : "启用")，选股结果待更新")
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: group.enabled ? "checkmark.circle.fill" : "circle")
                    Text(group.name).lineLimit(1)
                }
                .font(.caption.weight(.semibold)).frame(width: 100, height: 44, alignment: .leading)
                .foregroundStyle(group.enabled ? AppStyle.accent : Color.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("条件组：\(group.name)")
            .accessibilityValue(group.enabled ? "已启用" : "已停用")
            ScrollView(.horizontal) {
                HStack(spacing: 6) {
                    ForEach(group.and, id: \.self) { id in conditionSwitch(id, group: group, excluded: false) }
                    ForEach(group.not, id: \.self) { id in conditionSwitch(id, group: group, excluded: true) }
                    if group.and.isEmpty && group.not.isEmpty {
                        Button("添加条件", systemImage: "plus", action: onEdit).font(.caption).frame(height: 44)
                    }
                }
            }
            .scrollIndicators(.visible)
        }
        .disabled(model.catalog == nil || model.requiresPresetConfirmation)
    }

    private func conditionSwitch(_ id: String, group: SelectionRuleGroup, excluded: Bool) -> some View {
        let key = group.id + "|" + (excluded ? "not:" : "") + id
        let enabled = !model.draft.disabled.contains(key)
        let name = model.catalog?.criteria.first(where: { $0.id == id })?.label ?? id
        let title = (excluded ? "排除 · " : "") + name
        return Button {
            model.setCriterionEnabled(groupID: group.id, criterionID: id, excluded: excluded, enabled: !enabled)
            model.onContextChange?("screener", "“\(title)”已\(enabled ? "停用" : "启用")，选股结果待更新")
        } label: {
            HStack(spacing: 5) {
                Image(systemName: enabled ? "checkmark.circle.fill" : "circle")
                Text(title).lineLimit(1)
            }
            .font(.caption.weight(.medium))
            .padding(.horizontal, 10).padding(.vertical, 8)
            .foregroundStyle(enabled ? (excluded ? AppStyle.up : AppStyle.accent) : Color.secondary)
            .background(enabled ? (excluded ? AppStyle.up : AppStyle.accent).opacity(0.08) : Color.white,
                        in: RoundedRectangle(cornerRadius: 8))
            .frame(minHeight: 44).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(!group.enabled)
        .accessibilityLabel(title)
        .accessibilityValue(enabled ? "已启用" : "已停用")
        .accessibilityHint("点按切换并更新选股结果")
    }
}

@MainActor
struct StockSelectionSidebar: View {
    @ObservedObject var model: StockSelectionModel
    let section: StockSelectionSection
    @Binding var selectedStockCode: String?
    @Binding var editorPresented: Bool
    let onOpenStock: (String) -> Void
    let onOverlayChange: (Bool) -> Void
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
        .sheet(isPresented: $editorPresented) { SelectionRuleEditor(model: model) }
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
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("选股方案", selection: Binding(get: { model.selectedPresetID }, set: { model.usePreset($0) })) {
                    Text("临时方案").tag(Optional<String>.none)
                    ForEach(model.library?.presets ?? []) { preset in Text(preset.name).tag(Optional(preset.id)) }
                }
                .labelsHidden().frame(maxWidth: .infinity, alignment: .leading)
                if let preset = model.selectedPreset {
                    Menu {
                        Button("编辑方案") { editorPresented = true }
                        Button("删除方案", role: .destructive) { deletingPreset = preset }
                    } label: { Image(systemName: "ellipsis.circle").font(.title3).frame(width: 36, height: 36) }
                    .disabled(!model.canWrite)
                }
            }
            HStack {
                Text("在工作区顶部直接切换条件").font(.caption2).foregroundStyle(.secondary)
                Spacer(minLength: 4)
                Button { Task { await model.run() } } label: {
                    if model.isEvaluating { ProgressView() } else { Label("选股", systemImage: "play.fill") }
                }
                .buttonStyle(.borderedProminent).disabled(model.isEvaluating || model.catalog == nil || model.validationMessage != nil || model.requiresPresetConfirmation)
            }
            if model.evaluation != nil && !model.resultsAreCurrent {
                Text("条件已修改，以下仍为上次结果。").font(.caption).foregroundStyle(.orange)
            }
            if let warnings = model.selectedPreset?.warnings, !warnings.isEmpty { SelectionWarnings(warnings: warnings) }
            if model.requiresPresetConfirmation {
                Text("旧方案需要确认：请编辑条件并保存后再运行。").font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(12)
    }

    private var activeEvaluation: SelectionEvaluation? { section == .watchlist ? model.watchEvaluation : model.evaluation }
    private var activeLoading: Bool { section == .watchlist ? model.isLoadingGroup : model.isEvaluating }

    @ViewBuilder private var results: some View {
        if let result = activeEvaluation {
            VStack(spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("\(result.resultCount) 只股票").font(.subheadline.weight(.semibold))
                        Text(result.asOf ?? "数据时间未知").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
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
                .padding(12)
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
                        Button {
                            if choosingStocks {
                                if model.selectedCodes.contains(stock.code) { model.selectedCodes.remove(stock.code) }
                                else { model.selectedCodes.insert(stock.code) }
                            } else { onOpenStock(stock.code) }
                        } label: {
                            HStack(spacing: 8) {
                                if choosingStocks {
                                    Image(systemName: model.selectedCodes.contains(stock.code) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(model.selectedCodes.contains(stock.code) ? AppStyle.accent : .secondary)
                                }
                                SelectionStockRow(stock: stock)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(selectedStockCode == stock.code && !choosingStocks ? AppStyle.accent.opacity(0.08) : Color.clear)
                        .contextMenu {
                            Button("打开股票") { onOpenStock(stock.code) }
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

private struct SelectionStockRow: View {
    let stock: SelectionStock
    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 4) {
                Text(stock.name).font(.subheadline.weight(.medium)).foregroundStyle(AppStyle.ink).lineLimit(1)
                Text(stock.code + (stock.sector.map { " · " + $0 } ?? "")).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                if let score = stock.score { Text("评分 \(score.formatted(.number.precision(.fractionLength(0...1))))").font(.caption2).foregroundStyle(.secondary) }
            }
            Spacer(minLength: 4)
            VStack(alignment: .trailing, spacing: 4) {
                Text(AppStyle.price(stock.price)).foregroundStyle(AppStyle.ink)
                Text(AppStyle.change(stock.changePct)).foregroundStyle(AppStyle.movement(stock.changePct))
            }
            .font(.caption).monospacedDigit()
        }.padding(.vertical, 6)
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

@MainActor
private struct SelectionRuleEditor: View {
    @ObservedObject var model: StockSelectionModel
    @Environment(\.dismiss) private var dismiss
    @State private var pickerTarget: CriterionPickerTarget?
    @State private var savingPreset = false
    @State private var saveAsNew = false
    @State private var presetName = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    SelectionStatusView(model: model)
                    if let message = model.validationMessage { Text(message).font(.caption).foregroundStyle(.red) }
                    if model.requiresPresetConfirmation {
                        Text("此旧方案含尚未迁移的条件，暂不能运行。请核对下面保留下来的条件；确认符合你的意图后，使用右上“方案 → 保存当前方案”明确确认。")
                            .font(.subheadline).foregroundStyle(.orange)
                        SelectionWarnings(warnings: model.selectedPreset?.warnings ?? [])
                    }
                    Text("组内条件需要同时满足（AND）；不同条件组满足任意一组即可（OR）。排除项命中时剔除，资料缺失不会被当作符合。").font(.subheadline).foregroundStyle(.secondary)
                    ForEach(model.draft.groups) { group in ruleGroup(group) }
                    Button("添加条件组（OR）", systemImage: "plus.circle") {
                        model.draft.groups.append(SelectionRuleGroup(name: "条件组 \(model.draft.groups.count + 1)"))
                    }.buttonStyle(.bordered)
                    if model.draft.groups.isEmpty {
                        Text("尚未添加条件组。空条件会返回全市场，请先添加你需要的条件。").font(.caption).foregroundStyle(.secondary)
                    }
                    if let result = model.evaluation, model.resultsAreCurrent {
                        Text("最近结果：\(result.passed) / \(result.total) 只；\(result.unknown ?? 0) 只资料不全。")
                            .font(.subheadline.weight(.medium))
                        if let history = result.history { SelectionHistoryView(history: history) }
                    }
                    Button {
                        Task { await model.run(includeHistory: true) }
                    } label: { Label("查看近期历史效果", systemImage: "clock.arrow.circlepath") }
                    .buttonStyle(.bordered).disabled(model.isEvaluating || model.catalog == nil || model.validationMessage != nil || model.requiresPresetConfirmation)
                    Button {
                        Task { await model.run(); if model.resultsAreCurrent { dismiss() } }
                    } label: {
                        HStack { Spacer(); if model.isEvaluating { ProgressView() }; Text("运行选股"); Spacer() }.padding(.vertical, 6)
                    }
                    .buttonStyle(.borderedProminent).disabled(model.isEvaluating || model.catalog == nil || model.validationMessage != nil || model.requiresPresetConfirmation)
                }
                .padding(24).frame(maxWidth: 820).frame(maxWidth: .infinity)
            }
            .background(AppStyle.canvas)
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
                        Button("恢复初始条件") { if let catalog = model.catalog { model.draft = catalog.defaults } }
                    } label: { Label("方案", systemImage: "square.and.arrow.down") }
                    .disabled(!model.canWrite)
                }
            }
            .sheet(item: $pickerTarget) { target in CriterionPicker(model: model, target: target) }
            .alert("保存选股方案", isPresented: $savingPreset) {
                TextField("方案名称", text: $presetName)
                Button("取消", role: .cancel) { }
                Button("保存") {
                    let name = presetName.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !name.isEmpty else { return }
                    let id = saveAsNew ? nil : model.selectedPresetID
                    Task {
                        if await model.mutate(operation: "preset.save", payload: SelectionMutationPayload(id: id, name: name, definition: model.draft)) {
                            model.selectedPresetID = model.lastSavedPresetID ?? id
                        }
                    }
                }
            }
        }
    }

    private func ruleGroup(_ group: SelectionRuleGroup) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                TextField("条件组名称", text: Binding(get: { model.draft.groups.first { $0.id == group.id }?.name ?? "" }, set: { value in
                    if let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) { model.draft.groups[index].name = value }
                })).font(.headline)
                Toggle("启用条件组", isOn: Binding(get: { model.draft.groups.first { $0.id == group.id }?.enabled ?? false }, set: { value in
                    if let index = model.draft.groups.firstIndex(where: { $0.id == group.id }) { model.draft.groups[index].enabled = value }
                })).labelsHidden()
                Button(role: .destructive) { model.removeRuleGroup(group.id) } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless).accessibilityLabel("删除条件组")
            }
            Text("同时满足以下条件").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(group.and, id: \.self) { criterion in criterionRow(criterion, group: group, excluded: false) }
            Button("添加条件", systemImage: "plus") { pickerTarget = CriterionPickerTarget(groupID: group.id, excluded: false) }
            Divider()
            Text("排除以下情况").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            ForEach(group.not, id: \.self) { criterion in criterionRow(criterion, group: group, excluded: true) }
            Button("添加排除项", systemImage: "minus.circle") { pickerTarget = CriterionPickerTarget(groupID: group.id, excluded: true) }
            if model.resultsAreCurrent, let effect = model.evaluation?.groups?.first(where: { $0.id == group.id }) {
                Text("本组通过 \(effect.baselinePassed ?? 0) 只；资料不全 \(effect.unknown ?? 0) 只")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(18).background(.white, in: RoundedRectangle(cornerRadius: 18))
    }

    private func criterionRow(_ id: String, group: SelectionRuleGroup, excluded: Bool) -> some View {
        let criterion = model.catalog?.criteria.first { $0.id == id }
        let disabledKey = group.id + "|" + (excluded ? "not:" : "") + id
        let enabled = !model.draft.disabled.contains(disabledKey)
        let effect = model.resultsAreCurrent ? model.evaluation?.groups?.first { $0.id == group.id } : nil
        let impact = excluded ? effect?.notImpacts?[id] : effect?.impacts?[id]
        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Toggle(criterion?.label ?? id, isOn: Binding(get: { !model.draft.disabled.contains(disabledKey) }, set: {
                    model.setCriterionEnabled(groupID: group.id, criterionID: id, excluded: excluded, enabled: $0)
                })).font(.subheadline)
                Button { model.setCriterion(groupID: group.id, criterionID: id, excluded: excluded, present: false) } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }.buttonStyle(.borderless).accessibilityLabel("移除条件")
            }
            if let description = criterion?.description, !description.isEmpty {
                Text(description).font(.caption).foregroundStyle(.secondary)
            }
            if let impact { Text("关闭此项将多出 \(impact) 只").font(.caption2).foregroundStyle(.secondary) }
            ForEach(criterion?.parameters ?? [], id: \.self) { parameterID in
                if let parameter = model.catalog?.parameters.first(where: { $0.id == parameterID }) {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(parameter.label).font(.caption)
                            if let minimum = parameter.minimum, let maximum = parameter.maximum {
                                Text("\(minimum.formatted()) – \(maximum.formatted())").font(.caption2).foregroundStyle(.tertiary)
                            }
                        }
                        Spacer()
                        TextField(parameter.label, value: Binding(get: { model.draft.parameters[parameterID] ?? parameter.defaultValue ?? 0 }, set: { value in
                            if value.isFinite { model.draft.parameters[parameterID] = value }
                        }), format: .number)
                        .keyboardType(.numbersAndPunctuation).multilineTextAlignment(.trailing)
                        .textFieldStyle(.roundedBorder).frame(width: 100).font(.caption.monospacedDigit())
                    }
                }
            }
            .disabled(!enabled)
        }
        .padding(10).background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 10))
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

private struct CriterionPickerTarget: Identifiable {
    let groupID: String
    let excluded: Bool
    var id: String { groupID + (excluded ? "not" : "and") }
}

@MainActor
private struct CriterionPicker: View {
    @ObservedObject var model: StockSelectionModel
    let target: CriterionPickerTarget
    @Environment(\.dismiss) private var dismiss
    @State private var search = ""
    private var criteria: [SelectionCriterion] {
        (model.catalog?.criteria ?? []).filter { search.isEmpty || $0.label.localizedCaseInsensitiveContains(search) || ($0.category?.localizedCaseInsensitiveContains(search) ?? false) }
    }
    var body: some View {
        NavigationStack {
            List(criteria) { criterion in
                Toggle(isOn: Binding(get: {
                    guard let group = model.draft.groups.first(where: { $0.id == target.groupID }) else { return false }
                    return (target.excluded ? group.not : group.and).contains(criterion.id)
                }, set: { model.setCriterion(groupID: target.groupID, criterionID: criterion.id, excluded: target.excluded, present: $0) })) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(criterion.label)
                        if let category = criterion.category { Text(category).font(.caption).foregroundStyle(.secondary) }
                    }
                }
            }
            .searchable(text: $search, prompt: "搜索条件")
            .navigationTitle(target.excluded ? "添加排除项" : "添加条件")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
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
