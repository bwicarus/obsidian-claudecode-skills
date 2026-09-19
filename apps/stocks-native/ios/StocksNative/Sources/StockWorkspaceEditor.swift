import SwiftUI

@MainActor
struct WorkspaceEditor: View {
    @ObservedObject var store: WorkspaceLayoutStore
    var onApply: (() -> Void)?
    @Environment(\.dismiss) private var dismiss
    @State private var draft: WorkspaceLayout
    @State private var selectedPageID: String
    @State private var saveError: String?
    @State private var confirmReset = false

    init(store: WorkspaceLayoutStore, onApply: (() -> Void)? = nil) {
        self.store = store
        self.onApply = onApply
        let initial = store.layout.sanitized()
        _draft = State(initialValue: initial)
        _selectedPageID = State(initialValue: initial.selectedPage?.id ?? "chart")
    }

    private var pageIndex: Int? { draft.pages.firstIndex { $0.id == selectedPageID } }
    private var currentCards: [WorkspaceCard] { pageIndex.map { draft.pages[$0].cards } ?? [] }
    private var availableKinds: [WorkspaceCardKind] {
        let existing = Set(currentCards.map(\.kind))
        return WorkspaceCardKind.allCases.filter { !existing.contains($0) }
    }

    var body: some View {
        NavigationStack {
            List {
                if let warning = store.loadWarning {
                    Section { Label(warning, systemImage: "exclamationmark.circle").font(.footnote) }
                }
                pageSection
                cardsSection
                librarySection
                Section {
                    Button("恢复默认布局", role: .destructive) { confirmReset = true }
                } footer: {
                    Text("布局保存在这台设备上，切换股票时沿用。点击保存后生效；取消会放弃本次更改。")
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(AppStyle.canvas)
            .environment(\.editMode, .constant(.active))
            .navigationTitle("自定义工作台")
            .navigationBarTitleDisplayMode(.inline)
            .tint(AppStyle.accent)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button("保存", action: save).fontWeight(.semibold) }
            }
            .confirmationDialog("恢复默认的三个页签和卡片排列？", isPresented: $confirmReset, titleVisibility: .visible) {
                Button("恢复默认", role: .destructive) {
                    draft = .defaultLayout
                    selectedPageID = draft.selectedPage?.id ?? "chart"
                }
            } message: { Text("点击保存后生效，已有图表笔迹不会被删除。") }
            .alert("布局未保存", isPresented: Binding(get: { saveError != nil }, set: { if !$0 { saveError = nil } })) {
                Button("好", role: .cancel) { saveError = nil }
            } message: { Text(saveError ?? "请重试。") }
            .onChange(of: selectedPageID) { _, id in draft.selectedPageID = id }
        }
    }

    private var pageSection: some View {
        Section {
            Picker("正在编辑", selection: $selectedPageID) {
                ForEach(draft.pages) { page in Text(page.title.isEmpty ? "未命名页签" : page.title).tag(page.id) }
            }
            TextField("页签名称", text: Binding(
                get: { pageIndex.map { draft.pages[$0].title } ?? "" },
                set: { title in
                    guard let index = pageIndex else { return }
                    draft.pages[index].title = String(title.prefix(WorkspaceLayout.maximumTitleLength))
                }
            ))
            .accessibilityLabel("页签名称，最多 24 个字")
            HStack {
                Button(action: addPage) { Label("添加页签", systemImage: "plus") }
                    .disabled(draft.pages.count >= WorkspaceLayout.maximumPages)
                Spacer()
                Button("删除页签", role: .destructive, action: removePage)
                    .disabled(draft.pages.count <= 1)
            }
            .buttonStyle(.borderless)
        } header: { Text("页签 · \(draft.pages.count)/\(WorkspaceLayout.maximumPages)") }
        footer: { Text("每个页签可以放不同的卡片；名称最多 24 个字。") }
    }

    @ViewBuilder
    private var cardsSection: some View {
        Section {
            if currentCards.isEmpty {
                Label("这个页签还没有卡片，请从下方添加。", systemImage: "rectangle.badge.plus")
                    .font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(currentCards) { card in cardRow(card) }
                    .onMove(perform: moveCards)
                    .onDelete(perform: deleteCards)
            }
        } header: { Text("卡片 · \(currentCards.count)/\(WorkspaceLayout.maximumCardsPerPage)") }
        footer: {
            Text("按住右侧把手拖动排序，或长按卡片使用上移、下移。关闭开关可隐藏卡片并保留位置；屏幕较窄时半宽卡片会自动占满一行。")
        }
    }

    private func cardRow(_ card: WorkspaceCard) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Toggle(isOn: cardVisibility(card.kind)) {
                Label(card.kind.title, systemImage: card.kind.symbol)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(card.isVisible ? AppStyle.ink : Color.secondary)
            }
            .accessibilityLabel("显示\(card.kind.title)")
            Picker("卡片宽度", selection: cardSpan(card.kind)) {
                ForEach(WorkspaceCardSpan.allCases) { span in Text(span.title).tag(span) }
            }
            .pickerStyle(.segmented)
            .accessibilityLabel("\(card.kind.title)宽度")
        }
        .padding(.vertical, 5)
        .contextMenu {
            Button { shiftCard(card.kind, offset: -1) } label: { Label("上移", systemImage: "arrow.up") }
                .disabled(currentCards.first?.kind == card.kind)
            Button { shiftCard(card.kind, offset: 1) } label: { Label("下移", systemImage: "arrow.down") }
                .disabled(currentCards.last?.kind == card.kind)
            Button(role: .destructive) { removeCard(card.kind) } label: { Label("移除卡片", systemImage: "trash") }
        }
        .accessibilityAction(named: Text("上移")) { shiftCard(card.kind, offset: -1) }
        .accessibilityAction(named: Text("下移")) { shiftCard(card.kind, offset: 1) }
    }

    private var librarySection: some View {
        Section {
            if availableKinds.isEmpty {
                Text("所有卡片均已添加到当前页签。").font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(availableKinds) { kind in
                    Button { addCard(kind) } label: {
                        HStack(spacing: 12) {
                            Image(systemName: kind.symbol).frame(width: 24).foregroundStyle(AppStyle.accent)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(kind.title).font(.subheadline.weight(.medium)).foregroundStyle(AppStyle.ink)
                                Text(kind.description).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "plus.circle").foregroundStyle(AppStyle.accent)
                        }
                        .padding(.vertical, 4)
                    }
                    .buttonStyle(.plain)
                    .disabled(currentCards.count >= WorkspaceLayout.maximumCardsPerPage)
                    .accessibilityLabel("添加\(kind.title)")
                }
            }
        } header: { Text("添加卡片") }
    }

    private func cardSpan(_ kind: WorkspaceCardKind) -> Binding<WorkspaceCardSpan> {
        Binding(
            get: { currentCards.first { $0.kind == kind }?.span ?? kind.defaultSpan },
            set: { span in updateCard(kind) { $0.span = span } }
        )
    }

    private func cardVisibility(_ kind: WorkspaceCardKind) -> Binding<Bool> {
        Binding(
            get: { currentCards.first { $0.kind == kind }?.isVisible ?? false },
            set: { visible in updateCard(kind) { $0.isVisible = visible } }
        )
    }

    private func updateCard(_ kind: WorkspaceCardKind, apply: (inout WorkspaceCard) -> Void) {
        guard let page = pageIndex, let card = draft.pages[page].cards.firstIndex(where: { $0.kind == kind }) else { return }
        apply(&draft.pages[page].cards[card])
    }

    private func addPage() {
        guard draft.pages.count < WorkspaceLayout.maximumPages else { return }
        var number = 1
        while draft.pages.contains(where: { $0.title == "自定义 \(number)" }) { number += 1 }
        let page = WorkspacePage(title: "自定义 \(number)")
        draft.pages.append(page)
        selectedPageID = page.id
        draft.selectedPageID = page.id
    }

    private func removePage() {
        guard draft.pages.count > 1, let page = pageIndex else { return }
        draft.pages.remove(at: page)
        selectedPageID = draft.pages[min(page, draft.pages.count - 1)].id
        draft.selectedPageID = selectedPageID
    }

    private func addCard(_ kind: WorkspaceCardKind) {
        guard let page = pageIndex,
              draft.pages[page].cards.count < WorkspaceLayout.maximumCardsPerPage,
              !draft.pages[page].cards.contains(where: { $0.kind == kind }) else { return }
        draft.pages[page].cards.append(WorkspaceCard(kind: kind))
    }

    private func removeCard(_ kind: WorkspaceCardKind) {
        guard let page = pageIndex else { return }
        draft.pages[page].cards.removeAll { $0.kind == kind }
    }

    private func moveCards(from source: IndexSet, to destination: Int) {
        guard let page = pageIndex else { return }
        draft.pages[page].cards.move(fromOffsets: source, toOffset: destination)
    }

    private func deleteCards(at offsets: IndexSet) {
        guard let page = pageIndex else { return }
        draft.pages[page].cards.remove(atOffsets: offsets)
    }

    private func shiftCard(_ kind: WorkspaceCardKind, offset: Int) {
        guard let page = pageIndex, let index = draft.pages[page].cards.firstIndex(where: { $0.kind == kind }) else { return }
        let target = index + offset
        guard draft.pages[page].cards.indices.contains(target) else { return }
        draft.pages[page].cards.swapAt(index, target)
    }

    private func save() {
        draft.selectedPageID = selectedPageID
        do {
            try store.commit(draft)
            onApply?()
            dismiss()
        } catch {
            saveError = "无法写入本机布局：\(error.localizedDescription)"
        }
    }
}
