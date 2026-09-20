import SwiftUI

/// Favorites are memberships in the existing account's watch groups.
@MainActor
struct StockFavoriteButton: View {
    @ObservedObject var model: StockSelectionModel
    let code: String
    let name: String
    @State private var showingGroups = false

    private var isFavorite: Bool {
        model.library?.groups.contains { ($0.codes ?? []).contains(code) } == true
    }

    var body: some View {
        Button { showingGroups = true } label: {
            Image(systemName: isFavorite ? "star.fill" : "star")
                .font(.system(size: 16))
                .foregroundStyle(isFavorite ? Color.orange : Color.secondary)
                .frame(width: 32, height: 32)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(name)，\(isFavorite ? "管理收藏" : "收藏到分组")")
        .accessibilityValue(isFavorite ? "已收藏" : "未收藏")
        .sheet(isPresented: $showingGroups) {
            StockFavoriteGroups(model: model, code: code, name: name)
                .presentationDetents([.medium, .large])
        }
    }
}

@MainActor
private struct StockFavoriteGroups: View {
    @ObservedObject var model: StockSelectionModel
    let code: String
    let name: String
    @Environment(\.dismiss) private var dismiss
    @State private var newName = ""

    private var memberGroups: [SelectionWatchGroup] {
        model.manualGroups.filter { ($0.codes ?? []).contains(code) }
    }
    private var canEdit: Bool { model.canWrite && !model.isLoading }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack {
                        Text(name).font(.headline)
                        Text(code).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    SelectionStatusView(model: model)
                }
                Section {
                    ForEach(model.manualGroups) { group in
                        Toggle(isOn: Binding(
                            get: { (group.codes ?? []).contains(code) },
                            set: { selected in
                                Task {
                                    await model.mutate(operation: selected ? "group.add" : "group.remove",
                                        payload: SelectionMutationPayload(id: group.id, codes: [code]))
                                }
                            }
                        )) {
                            HStack {
                                Label(group.name, systemImage: "folder")
                                Spacer()
                                Text("\(group.codes?.count ?? 0)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            }
                        }.disabled(!canEdit)
                    }
                    if model.manualGroups.isEmpty {
                        Text("还没有收藏夹，可在下方新建。").foregroundStyle(.secondary)
                    }
                } header: { Text("收藏夹 · 可多选") } footer: {
                    Text("切换后自动保存。同一只股票可以属于多个分组，全部取消即移出手动收藏。")
                }
                let smartGroups = model.library?.groups.filter(\.isSmart) ?? []
                if !smartGroups.isEmpty {
                    Section("智能分组 · 按规则自动管理") {
                        ForEach(smartGroups) { group in
                            Label(group.name, systemImage: "sparkles").foregroundStyle(.secondary)
                        }
                    }
                }
                Section("新建收藏夹并加入") {
                    TextField("收藏夹名称", text: $newName)
                        .disabled(!canEdit)
                        .onChange(of: newName) { _, value in
                            if value.count > 60 { newName = String(value.prefix(60)) }
                        }
                    Button("新建并收藏", systemImage: "folder.badge.plus") {
                        let value = newName.trimmingCharacters(in: .whitespacesAndNewlines)
                        Task {
                            if await model.mutate(operation: "group.create",
                                payload: SelectionMutationPayload(name: value, kind: "manual", codes: [code])) {
                                newName = ""
                            }
                        }
                    }.disabled(!canEdit || newName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                if !memberGroups.isEmpty {
                    Section {
                        Button("移出全部手动收藏夹", role: .destructive) {
                            let ids = memberGroups.map(\.id)
                            Task {
                                await model.mutate(operation: "group.remove",
                                    payload: SelectionMutationPayload(groupIds: ids, codes: [code]))
                            }
                        }.disabled(!canEdit)
                    }
                }
            }
            .navigationTitle("收藏分组")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }.disabled(model.isMutating)
                }
            }
            .task { await model.refresh() }
            .interactiveDismissDisabled(model.isMutating)
        }
    }
}
