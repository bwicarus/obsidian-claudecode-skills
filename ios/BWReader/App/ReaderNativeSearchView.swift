import SwiftUI

@MainActor
final class ReaderNativeSearchModel: ObservableObject, Identifiable {
    let id = UUID()
    let scope: String
    private let request: ([String: Any]) async -> [String: Any]
    private var queryTicket = UUID()
    @Published var query = ""
    @Published private(set) var results: [ReaderNativeSearchResult] = []
    @Published private(set) var total = 0
    @Published private(set) var pages = 0
    @Published private(set) var incomplete = false
    @Published private(set) var loading = false
    @Published private(set) var jumping = false
    @Published private(set) var error: String?

    init(scope: String, request: @escaping ([String: Any]) async -> [String: Any]) {
        self.scope = scope
        self.request = request
    }

    func search() async {
        let ticket = UUID()
        queryTicket = ticket
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        results = []; error = nil; total = 0; pages = 0; incomplete = false
        loading = !text.isEmpty
        defer { if queryTicket == ticket { loading = false } }
        if !text.isEmpty {
            do { try await Task.sleep(for: .milliseconds(280)) } catch { return }
        }
        guard !Task.isCancelled else { return }
        let receipt = await request(["action": "searchRead", "scope": scope, "text": text])
        guard !Task.isCancelled, queryTicket == ticket else { return }
        guard receipt["ok"] as? Bool == true, let value = receipt["value"] as? [String: Any] else {
            error = receipt["error"] as? String ?? "搜索失败，请重试。"
            return
        }
        results = (value["results"] as? [[String: Any]] ?? []).compactMap(ReaderNativeSearchResult.init)
        total = (value["total"] as? NSNumber)?.intValue ?? 0
        pages = (value["pages"] as? NSNumber)?.intValue ?? 0
        incomplete = value["incomplete"] as? Bool ?? false
    }

    func jump(_ result: ReaderNativeSearchResult) async -> Bool {
        guard !jumping, !loading, results.contains(where: { $0.id == result.id }) else { return false }
        jumping = true
        defer { jumping = false }
        let receipt = await request(["action": "searchJump", "scope": scope, "actionId": result.id])
        if receipt["ok"] as? Bool == true { return true }
        error = receipt["error"] as? String ?? "无法打开这条结果，请重新搜索。"
        return false
    }
}

struct ReaderNativeSearchResult: Identifiable {
    let id: String
    let label: String
    let excerpt: String
    let count: Int
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        label = value["label"] as? String ?? ""
        excerpt = value["excerpt"] as? String ?? ""
        count = (value["count"] as? NSNumber)?.intValue ?? 1
    }
}

@MainActor
struct ReaderNativeSearchView: View {
    @ObservedObject var model: ReaderNativeSearchModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                if let error = model.error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                if model.loading { ProgressView("搜索中…") }
                else if !model.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Section {
                        Text("\(model.total) 处" + (model.pages > 0 ? " · \(model.pages) 页" : ""))
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        if model.incomplete {
                            Text("仍有部分页面待识别；以下是已完成页面的搜索结果。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if model.results.isEmpty && model.error == nil {
                            Text(model.incomplete ? "已识别页面暂无匹配" : "未找到匹配内容")
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                ForEach(model.results) { result in
                    Button {
                        Task { if await model.jump(result) { dismiss() } }
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(result.label + (result.count > 1 ? " · \(result.count) 处" : ""))
                                .font(.caption.weight(.semibold)).foregroundStyle(ReaderNativeTheme.accent)
                            Text(highlight(result.excerpt)).font(.subheadline).foregroundStyle(ReaderNativeTheme.ink)
                        }.padding(.vertical, 4)
                    }.disabled(model.jumping || model.loading)
                }
            }
            .scrollContentBackground(.hidden).background(ReaderNativeTheme.canvas)
            .searchable(text: $model.query, placement: .navigationBarDrawer(displayMode: .always), prompt: "搜索当前书籍")
            .task(id: model.query) { await model.search() }
            .navigationTitle("全文搜索").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }.tint(ReaderNativeTheme.accent)
    }

    private func highlight(_ text: String) -> AttributedString {
        var value = AttributedString(text)
        let query = model.query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return value }
        var remaining = text.startIndex..<text.endIndex
        while let range = text.range(of: query, options: [.caseInsensitive], range: remaining) {
            if let lower = AttributedString.Index(range.lowerBound, within: value),
               let upper = AttributedString.Index(range.upperBound, within: value) {
                value[lower..<upper].backgroundColor = ReaderNativeTheme.accentWash
            }
            remaining = range.upperBound..<text.endIndex
        }
        return value
    }
}
