import SwiftUI

/// 卡片收藏夹里的一张（数据来自 rc-voicecall 的收藏夹，经 favoritesList 取来）。
struct ReaderNativeFavorite: Identifiable {
    let id: String
    let label: String
    let isCards: Bool
    let isHtml: Bool
    let content: String
    let cards: [[String: String]]
    let page: String
    let file: String

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        label = value["label"] as? String ?? "收藏卡片"
        isCards = value["kind"] as? String == "cards"
        isHtml = value["isHtml"] as? Bool ?? false
        content = value["content"] as? String ?? ""
        cards = (value["cards"] as? [[String: Any]] ?? []).map { card in
            card.compactMapValues { $0 as? String }
        }
        page = value["page"] as? String ?? ""
        file = value["file"] as? String ?? ""
    }

    /// 普通卡交给页卡同一个正文渲染器（ReaderNativePageCardBody）。
    var part: ReaderNativeConversationPart? {
        ReaderNativeConversationPart([
            "id": "favorite-" + id, "kind": "general", "title": label, "text": "", "status": "saved",
            "data": ["text": content, "format": isHtml ? "html" : "text"],
        ])
    }
}

/// 原版 `#vc-dock-btn`：右下角的收藏夹按钮，收藏夹里有卡才出现，带数目。
struct ReaderNativeFavoritesButton: View {
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var showing = false

    var body: some View {
        if model.favoritesCount > 0 {
            Button { showing = true } label: {
                HStack(spacing: 5) {
                    Image(systemName: "tray.and.arrow.down")
                        .font(.system(size: 15, weight: .medium))
                    Text("\(model.favoritesCount)")
                        .font(.system(size: 12, weight: .semibold).monospacedDigit())
                }
                .foregroundStyle(Color(red: 0xdd / 255, green: 0xe6 / 255, blue: 0xf5 / 255))
                .padding(.horizontal, 12).padding(.vertical, 9)
                .readerGlass(in: Capsule(), fallback: Color(red: 30 / 255, green: 32 / 255, blue: 42 / 255).opacity(0.94))
                .overlay(Capsule().stroke(Color(red: 126 / 255, green: 171 / 255, blue: 1).opacity(0.48), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("卡片收藏夹，\(model.favoritesCount) 张")
            .sheet(isPresented: $showing) {
                ReaderNativeFavoritesView(reader: reader, model: model)
            }
        }
    }
}

/// 原版 `#vc-dock-panel` 的原生版：列出收藏的卡片。
/// 原版"向上拖出 = 复制到屏幕"，这里是「放到当前页」（同样是复制，收藏夹里那张不动）。
struct ReaderNativeFavoritesView: View {
    @ObservedObject var reader: ReaderWebViewModel
    @ObservedObject var model: ReaderNativeConversationModel
    @Environment(\.dismiss) private var dismiss
    @State private var items: [ReaderNativeFavorite] = []
    @State private var loading = true
    @State private var confirmDelete: ReaderNativeFavorite?

    var body: some View {
        NavigationStack {
            Group {
                if loading {
                    ProgressView("正在读取收藏夹").frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if items.isEmpty {
                    ContentUnavailableView("收藏夹是空的", systemImage: "tray",
                                           description: Text("把卡片拖到屏幕底边就能收进来。"))
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 12) {
                            ForEach(items) { item in row(item) }
                        }
                        .padding(16)
                    }
                }
            }
            .navigationTitle("卡片收藏夹")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
        }
        .task { await reload() }
        .confirmationDialog("从收藏夹删除这张卡？（1 天内可在网页收藏夹的回收站恢复）",
                            isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
                            titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                guard let item = confirmDelete else { return }
                Task {
                    if await reader.deleteNativeFavorite(item) { items.removeAll { $0.id == item.id } }
                }
            }
        }
    }

    private func reload() async {
        loading = true
        items = await reader.loadNativeFavorites()
        loading = false
    }

    @ViewBuilder
    private func row(_ item: ReaderNativeFavorite) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: item.isCards ? "rectangle.on.rectangle" : "doc.text")
                    .foregroundStyle(ReaderNativeTheme.accent)
                Text(item.label).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer(minLength: 0)
                if !item.page.isEmpty {
                    Text("p.\(item.page)").font(.caption2).foregroundStyle(ReaderNativeTheme.muted)
                }
            }
            if item.isCards {
                ForEach(Array(item.cards.prefix(6).enumerated()), id: \.offset) { _, card in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(card["front"] ?? card["question"] ?? card["cloze"] ?? card["text"] ?? "")
                            .font(.footnote.weight(.medium))
                        Text(card["back"] ?? card["answer"] ?? "")
                            .font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                    }
                }
            } else if let part = item.part {
                ReaderNativePageCardBody(parts: [part], model: model)
                    .frame(maxHeight: 260, alignment: .top)
                    .clipped()
            }
            HStack(spacing: 10) {
                Button {
                    Task { if await reader.placeNativeFavorite(item) { dismiss() } }
                } label: {
                    Label("放到当前页", systemImage: "rectangle.portrait.and.arrow.forward")
                }
                .buttonStyle(.borderedProminent)
                .disabled(reader.nativePDFDocument == nil)
                Button(role: .destructive) { confirmDelete = item } label: {
                    Label("删除", systemImage: "trash")
                }
                .buttonStyle(.bordered)
            }
            .font(.footnote)
        }
        .padding(12)
        .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 12))
    }
}
