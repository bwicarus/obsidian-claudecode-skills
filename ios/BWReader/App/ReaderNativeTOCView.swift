import SwiftUI

@MainActor
final class ReaderNativeTOCModel: ObservableObject, Identifiable {
    let id = UUID()
    private let scope: String
    private let request: ([String: Any]) async -> [String: Any]
    @Published private(set) var entries: [ReaderNativeTOCEntry] = []
    @Published private(set) var loading = false
    @Published private(set) var jumping = false
    @Published private(set) var selectedID: String?
    @Published private(set) var error: String?

    init(scope: String, request: @escaping ([String: Any]) async -> [String: Any]) {
        self.scope = scope
        self.request = request
    }

    func load() async {
        guard !loading else { return }
        loading = true; error = nil; entries = []; selectedID = nil
        defer { loading = false }
        let receipt = await request(["action": "tocRead", "scope": scope])
        guard !Task.isCancelled else { return }
        guard receipt["ok"] as? Bool == true, let value = receipt["value"] as? [[String: Any]] else {
            error = receipt["error"] as? String ?? "目录加载失败，请重试。"
            return
        }
        entries = value.compactMap(ReaderNativeTOCEntry.init)
    }

    func jump(_ entry: ReaderNativeTOCEntry) async -> Bool {
        guard !loading, !jumping, entries.contains(where: { $0.id == entry.id }) else { return false }
        jumping = true; error = nil
        defer { jumping = false }
        let receipt = await request(["action": "tocJump", "scope": scope, "actionId": entry.id])
        guard receipt["ok"] as? Bool == true else {
            error = receipt["error"] as? String ?? "无法跳转，请刷新目录。"
            return false
        }
        selectedID = entry.id
        return true
    }
}

struct ReaderNativeTOCEntry: Identifiable {
    let id: String
    let title: String
    let label: String
    let level: Int

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        title = value["title"] as? String ?? ""
        label = value["label"] as? String ?? ""
        level = min(12, max(1, (value["level"] as? NSNumber)?.intValue ?? 1))
    }
}

@MainActor
struct ReaderNativeTOCView: View {
    @ObservedObject var model: ReaderNativeTOCModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.horizontalSizeClass) private var sizeClass
    @State private var query = ""

    private var entries: [ReaderNativeTOCEntry] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return q.isEmpty ? model.entries : model.entries.filter { $0.title.localizedCaseInsensitiveContains(q) }
    }

    var body: some View {
        NavigationStack {
            List {
                if let error = model.error {
                    Text(error).foregroundStyle(.red).textSelection(.enabled)
                    Button("重新加载") { Task { await model.load() } }.disabled(model.loading || model.jumping)
                }
                if model.loading { ProgressView("加载目录…") }
                else if entries.isEmpty && model.error == nil {
                    Text(model.entries.isEmpty ? "这本书还没有目录" : "没有匹配的章节").foregroundStyle(.secondary)
                }
                ForEach(entries) { entry in
                    Button {
                        Task {
                            if await model.jump(entry), sizeClass == .compact { dismiss() }
                        }
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            Text(entry.title).font(.subheadline).foregroundStyle(ReaderNativeTheme.ink)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            if !entry.label.isEmpty {
                                Text(entry.label).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            }
                            if model.selectedID == entry.id {
                                Image(systemName: "checkmark").font(.caption.weight(.semibold))
                            }
                        }.padding(.leading, CGFloat(min(entry.level - 1, 6)) * 12)
                    }
                    .disabled(model.loading || model.jumping)
                    .listRowBackground(model.selectedID == entry.id ? ReaderNativeTheme.accentWash : Color.clear)
                }
            }
            .scrollContentBackground(.hidden).background(ReaderNativeTheme.canvas)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "查找章节")
            .navigationTitle("书籍目录").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { Task { await model.load() } } label: { Image(systemName: "arrow.clockwise") }
                        .disabled(model.loading || model.jumping).accessibilityLabel("刷新目录")
                }
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task { await model.load() }
        }.tint(ReaderNativeTheme.accent)
    }
}
