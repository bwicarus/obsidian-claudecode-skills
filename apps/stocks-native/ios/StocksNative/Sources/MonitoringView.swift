import SwiftUI

@MainActor
struct MonitoringView: View {
    @ObservedObject var model: MonitoringModel
    let destination: MonitoringDestination
    let onOpenStock: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var tab: String
    @State private var editingRule: MonitoringRule?
    @State private var deletingRule: MonitoringRule?
    @State private var showResolved = false
    @State private var focusedInitialNotice = false

    init(model: MonitoringModel, destination: MonitoringDestination, onOpenStock: @escaping (String) -> Void) {
        self.model = model; self.destination = destination; self.onOpenStock = onOpenStock
        _tab = State(initialValue: destination.tab)
    }

    private var rules: [MonitoringRule] {
        model.library?.rules.filter { destination.code == nil || $0.code == destination.code } ?? []
    }

    private var notifications: [MonitoringNotice] {
        model.library?.notifications.filter {
            (destination.code == nil || $0.code == destination.code)
                && (showResolved || !$0.isResolved || $0.id == destination.notificationId)
        } ?? []
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("盯盘内容", selection: $tab) {
                    Text("通知 \(model.unreadCount > 0 ? "· \(model.unreadCount)" : "")").tag("notifications")
                    Text("规则").tag("rules")
                }
                .pickerStyle(.segmented).padding()
                ScrollViewReader { proxy in
                    List {
                        statusSection
                        if tab == "rules" { rulesSection } else { notificationsSection }
                    }
                    .listStyle(.insetGrouped)
                    .refreshable { await model.refresh() }
                    .onChange(of: model.library?.notifications.count, initial: true) { _, _ in
                        if !focusedInitialNotice, let id = destination.notificationId,
                           notifications.contains(where: { $0.id == id }) {
                            focusedInitialNotice = true
                            proxy.scrollTo(id, anchor: .top)
                        }
                    }
                }
            }
            .navigationTitle(destination.code.map { "\($0) · 盯盘" } ?? "盯盘与通知")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .primaryAction) {
                    Button("新建规则", systemImage: "plus") {
                        editingRule = .draft(code: destination.code ?? "")
                    }.disabled(!model.canWrite)
                }
            }
        }
        .sheet(item: $editingRule) { rule in MonitoringRuleEditor(model: model, initial: rule) }
        .alert("删除这条盯盘规则？", isPresented: Binding(get: { deletingRule != nil }, set: { if !$0 { deletingRule = nil } })) {
            Button("取消", role: .cancel) { deletingRule = nil }
            Button("删除规则", role: .destructive) {
                guard let rule = deletingRule else { return }
                Task { if await model.mutate(operation: "rule.delete", id: rule.id) { deletingRule = nil } }
            }
        } message: { Text("已经触发的提醒仍保留在通知记录中。") }
    }

    @ViewBuilder private var statusSection: some View {
        if let error = model.error {
            Section {
                Text(error).font(.footnote).foregroundStyle(.red)
                if model.pendingMutation != nil {
                    Button("重试确认上次操作") { Task { await model.retryPending() } }.disabled(model.isMutating)
                } else {
                    Button("重新同步") { Task { await model.refresh() } }.disabled(model.isRefreshing)
                }
            }
        } else if model.pendingMutation != nil {
            Section {
                Button("确认上次操作结果") { Task { await model.retryPending() } }.disabled(model.isMutating)
            }
        }
        if !model.isFresh, model.library != nil {
            Section { Text("显示本机保存的记录，等待服务器同步。暂时不能修改规则。")
                .font(.caption).foregroundStyle(.secondary) }
        }
        if model.isRefreshing && model.library == nil {
            Section { ProgressView("同步盯盘记录…") }
        }
    }

    @ViewBuilder private var rulesSection: some View {
        if rules.isEmpty {
            Section {
                ContentUnavailableView("还没有盯盘规则", systemImage: "waveform.path.ecg",
                                       description: Text("设置指标与阈值，达到条件后自动交给 AI 分析并提醒你。"))
                Button("创建第一条规则", systemImage: "plus") { editingRule = .draft(code: destination.code ?? "") }
                    .disabled(!model.canWrite)
            }
        } else {
            ForEach(rules) { rule in
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text(rule.title).font(.headline)
                            Spacer()
                            Text(rule.stateTitle).font(.caption).foregroundStyle(rule.enabled ? AppStyle.accent : .secondary)
                        }
                        Text(rule.code).font(.caption.monospaced()).foregroundStyle(.secondary)
                        Text(rule.conditionDescription).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                        HStack {
                            MonitoringSeverityLabel(severity: rule.severity)
                            Text("持续 \(rule.confirmSeconds.formatted()) 秒 · 冷却 \((rule.cooldownSeconds / 60).formatted()) 分钟")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        HStack(spacing: 20) {
                            Button("编辑") { editingRule = rule }
                            Button(rule.enabled ? "暂停" : "启用") {
                                Task { await model.mutate(operation: rule.enabled ? "rule.pause" : "rule.resume", id: rule.id) }
                            }
                            Spacer()
                            Button("删除", role: .destructive) { deletingRule = rule }
                        }.font(.caption).buttonStyle(.borderless).disabled(!model.canWrite)
                    }.padding(.vertical, 4)
                }
            }
        }
    }

    @ViewBuilder private var notificationsSection: some View {
        Section {
            MonitoringNotificationPermission()
            Toggle("显示已处理记录", isOn: $showResolved).font(.subheadline)
        } footer: { Text("已读表示看过提醒；已处理表示这件事已经处理完成。") }
        if notifications.isEmpty {
            Section { ContentUnavailableView("暂无提醒", systemImage: "bell", description: Text("规则触发和 AI 分析结果会保留在这里。")) }
        } else {
            ForEach(notifications) { notice in
                Section {
                    MonitoringNoticeCard(notice: notice, model: model, onOpenStock: { code in
                        dismiss(); onOpenStock(code)
                    })
                }.id(notice.id)
            }
        }
    }
}

@MainActor
private struct MonitoringRuleEditor: View {
    @ObservedObject var model: MonitoringModel
    let initial: MonitoringRule
    @Environment(\.dismiss) private var dismiss
    @State private var draft: MonitoringRule
    @State private var validation: String?
    @State private var accountScope: String

    init(model: MonitoringModel, initial: MonitoringRule) {
        self.model = model; self.initial = initial; _draft = State(initialValue: initial)
        _accountScope = State(initialValue: model.accountScope)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("监测对象") {
                    TextField("规则名称", text: $draft.title)
                    TextField("六位股票代码", text: $draft.code).keyboardType(.numberPad)
                    Toggle("启用规则", isOn: $draft.enabled)
                }
                Section {
                    if draft.conditions.count > 1 {
                        Picker("触发方式", selection: $draft.match) {
                            Text("全部条件满足").tag("all")
                            Text("任一条件满足").tag("any")
                        }
                    }
                    ForEach(draft.conditions.indices, id: \.self) { index in
                        conditionEditor(index)
                    }
                    Button("添加条件", systemImage: "plus") {
                        draft.conditions.append(.init(metric: "changePct", op: "above", threshold: 3))
                    }.disabled(draft.conditions.count >= 12)
                } header: { Text("触发条件") } footer: {
                    Text("条件需持续满足后才触发。行情缺失或过期时不作判定；触发后需条件恢复才会重新待命。")
                }
                Section("频率与提醒") {
                    Stepper("确认时间：\(draft.confirmSeconds.formatted()) 秒", value: $draft.confirmSeconds, in: 0...3600, step: 5)
                    Stepper("冷却时间：\((draft.cooldownSeconds / 60).formatted()) 分钟", value: $draft.cooldownSeconds, in: 0...86400, step: 60)
                    LabeledContent("恢复缓冲（%）") {
                        TextField("0.05", value: $draft.rearmPercent, format: .number)
                            .keyboardType(.decimalPad).multilineTextAlignment(.trailing).frame(width: 90)
                    }
                    Picker("提醒等级", selection: $draft.severity) {
                        Text("普通").tag("normal")
                        Text("重要").tag("important")
                        Text("紧急").tag("urgent")
                    }
                    Text("所有等级都保留视觉提醒；系统通知和来电受设备权限及在线状态影响。")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("恢复缓冲：涨跌幅等百分比指标使用百分点；价格和量比使用阈值的相对百分比。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let message = validation ?? model.error {
                    Section { Text(message).font(.footnote).foregroundStyle(.red) }
                }
                if model.pendingMutation != nil {
                    Section {
                        Button("重试确认保存") {
                            Task { if await model.retryPending() { dismiss() } }
                        }.disabled(model.isMutating)
                    }
                }
            }
            .navigationTitle(model.library?.rules.contains(where: { $0.id == initial.id }) == true ? "编辑规则" : "新建规则")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存", action: save).disabled(!model.canWrite)
                }
            }
        }
    }

    private func conditionEditor(_ index: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("指标", selection: $draft.conditions[index].metric) {
                ForEach(model.catalog?.metrics ?? MonitoringMetric.defaults) { metric in
                    Text(metric.label).tag(metric.id)
                }
            }
            Picker("条件", selection: $draft.conditions[index].op) {
                Text("达到或高于").tag("above")
                Text("达到或低于").tag("below")
            }.pickerStyle(.segmented)
            LabeledContent("阈值") {
                TextField("阈值", value: $draft.conditions[index].threshold, format: .number)
                    .keyboardType(.numbersAndPunctuation).multilineTextAlignment(.trailing).frame(maxWidth: 150)
            }
            if draft.conditions.count > 1 {
                Button("移除此条件", role: .destructive) { draft.conditions.remove(at: index) }.font(.caption)
            }
        }.padding(.vertical, 4)
    }

    private func save() {
        guard model.accountScope == accountScope else {
            validation = "账户已变更，请重新打开这条规则。"; return
        }
        draft.code = draft.code.trimmingCharacters(in: .whitespacesAndNewlines)
        draft.title = draft.title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard draft.code.count == 6, draft.code.allSatisfy({ $0.isASCII && $0.isNumber }) else {
            validation = "请输入六位股票代码。"; return
        }
        guard !draft.conditions.isEmpty, draft.conditions.allSatisfy({ $0.threshold.isFinite }),
              draft.rearmPercent.isFinite, (0...50).contains(draft.rearmPercent) else {
            validation = "请检查条件和恢复缓冲的数值。"; return
        }
        if draft.title.isEmpty { draft.title = "\(draft.code) · \(draft.conditions[0].display)" }
        validation = nil
        Task { if await model.mutate(operation: "rule.upsert", rule: draft) { dismiss() } }
    }
}

@MainActor
private struct MonitoringNotificationPermission: View {
    @ObservedObject private var coordinator = StockNotificationCoordinator.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button(coordinator.notificationsEnabled ? "检查系统通知权限" : "启用系统通知", systemImage: "bell.badge") {
                Task { await coordinator.requestAuthorization() }
            }
            Text(coordinator.status).font(.caption).foregroundStyle(.secondary)
        }
    }
}

@MainActor
private struct MonitoringNoticeCard: View {
    let notice: MonitoringNotice
    @ObservedObject var model: MonitoringModel
    let onOpenStock: (String) -> Void
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                MonitoringSeverityLabel(severity: notice.severity)
                Spacer()
                Text(notice.statusTitle).font(.caption).foregroundStyle(notice.isUnread ? AppStyle.accent : .secondary)
            }
            Text(notice.title).font(.headline)
            if !notice.body.isEmpty { Text(notice.body).font(.subheadline).textSelection(.enabled) }
            if let aiTitle = notice.aiTitle {
                Label(aiTitle, systemImage: notice.aiState == "running" ? "hourglass" : "sparkles")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text(notice.displayTime).font(.caption2).foregroundStyle(.secondary)
            HStack(spacing: 16) {
                if let code = notice.stockCode { Button("查看 \(code)") { onOpenStock(code) } }
                Spacer(minLength: 0)
                if notice.isUnread {
                    Button("标为已读") { Task { await model.mutate(operation: "notification.read", id: notice.id) } }
                        .disabled(!model.canWrite)
                }
                if !notice.isResolved {
                    Button("已处理", systemImage: "checkmark.circle") {
                        Task { await model.mutate(operation: "notification.resolve", id: notice.id) }
                    }.disabled(!model.canWrite)
                }
            }.font(.caption).buttonStyle(.borderless)
        }
        .padding(.vertical, 4)
        .task(id: "\(scenePhase):\(notice.id)") {
            if scenePhase == .active { await model.recordVisualReceipt(notice.id) }
        }
    }
}

struct MonitoringSeverityLabel: View {
    let severity: String
    var body: some View {
        Label(["normal": "普通", "important": "重要", "urgent": "紧急"][severity] ?? severity,
              systemImage: severity == "urgent" ? "exclamationmark.circle.fill" : "bell.fill")
            .font(.caption.weight(.medium))
            .foregroundStyle(severity == "urgent" ? AppStyle.up : severity == "important" ? Color.orange : AppStyle.accent)
    }
}

@MainActor
struct MonitoringBanner: View {
    let notice: MonitoringNotice
    @ObservedObject var model: MonitoringModel
    let onOpen: () -> Void
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Button(action: onOpen) {
                HStack(spacing: 10) {
                    Image(systemName: notice.severity == "urgent" ? "exclamationmark.bubble.fill" : "bell.badge.fill")
                        .foregroundStyle(notice.severity == "urgent" ? AppStyle.up : AppStyle.accent)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(notice.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text(notice.body.isEmpty ? (notice.aiTitle ?? "规则已触发，点按查看") : notice.body)
                            .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                    Spacer(minLength: 0)
                    if model.unreadCount > 1 { Text("+\(model.unreadCount - 1)").font(.caption.monospacedDigit()) }
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            Button { model.dismissBanner(notice.id) } label: { Image(systemName: "xmark").font(.caption).frame(width: 36, height: 40) }
                .buttonStyle(.plain).accessibilityLabel("收起横幅，提醒仍保留未读")
        }
        .padding(.leading, 14).padding(.trailing, 4).padding(.vertical, 9)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
        .overlay { RoundedRectangle(cornerRadius: 12).strokeBorder(AppStyle.accent.opacity(0.15)) }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .task(id: "\(scenePhase):\(notice.id)") {
            if scenePhase == .active { await model.recordVisualReceipt(notice.id) }
        }
    }
}

struct MonitoringStockRow: View {
    @EnvironmentObject private var research: StockResearchStore
    @State private var showingResearch = false
    let record: StockTableRecord
    let columns: [StockTableColumn]
    @ObservedObject var selection: StockSelectionModel
    let summary: MonitoringSummary?
    var isSelected: Bool? = nil
    let onOpen: () -> Void
    let onMonitoring: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            StockFavoriteButton(model: selection, code: record.code, name: record.name)
                .frame(width: 32, height: 38)
            ForEach(columns) { column in
                Group {
                    if column == .monitoring {
                        monitorBadge
                    } else if column == .strategy {
                        StockStrategyBadge(signal: research.signals[record.code]) { showingResearch = true }
                    } else {
                        Button(action: onOpen) {
                            cell(column)
                                .frame(maxWidth: .infinity, minHeight: 40, alignment: column.alignment)
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 6)
                .frame(width: column.width, alignment: column.alignment)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .sheet(isPresented: $showingResearch) {
            StockRowResearchView(code: record.code, name: record.name, research: research)
        }
    }

    @ViewBuilder private func cell(_ column: StockTableColumn) -> some View {
        if column == .identity {
            HStack(spacing: 5) {
                if let isSelected {
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(isSelected ? AppStyle.accent : .secondary)
                }
                VStack(alignment: .leading, spacing: 1) {
                    Text(record.name).font(.subheadline.weight(.medium)).foregroundStyle(AppStyle.ink).lineLimit(1)
                    Text(record.code).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
        } else {
            Text(record.text(column)).font(.caption).monospacedDigit().lineLimit(1)
                .foregroundStyle(column == .changePct ? AppStyle.movement(record.values[.changePct]) : AppStyle.ink)
                .accessibilityLabel("\(column.title)，\(record.text(column))")
        }
    }

    private var monitorBadge: some View {
        Button(action: onMonitoring) {
            HStack(spacing: 3) {
                Image(systemName: (summary?.unreadCount ?? 0) > 0 ? "bell.badge.fill" : "waveform.path.ecg")
                Text(summary?.title ?? "盯盘")
            }
            .font(.caption2.weight(.medium))
            .foregroundStyle((summary?.unreadCount ?? 0) > 0 ? AppStyle.up : AppStyle.accent)
            .lineLimit(1).padding(.horizontal, 5).padding(.vertical, 3)
            .background(AppStyle.accent.opacity(0.07), in: RoundedRectangle(cornerRadius: 5))
            .frame(maxWidth: .infinity, minHeight: 36)
            .contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("\(record.name) \(summary?.title ?? "创建盯盘规则")")
    }
}
