import SwiftUI

struct StockNewsResponse: Codable {
    let category: String
    let items: [StockNewsItem]
    let status: String
    let fetchedAt: String?
    let asOf: String?
    let warnings: [String]
}

struct StockNewsItem: Codable, Identifiable {
    let id: String
    let title: String
    let summary: String?
    let publishedAt: String?
    let source: String
    let url: String?
    let isLegacy: Bool
}

struct StockNewsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var category = "macro"
    @State private var sector = ""
    @State private var response: StockNewsResponse?
    @State private var error: String?
    @State private var loading = false
    @State private var requestID = UUID()
    private var identity: String { "\(model.planScopeID):\(category):\(model.selectedCode ?? "")" }

    var body: some View {
        NavigationStack {
            VStack(spacing: 10) {
                Picker("新闻范围", selection: $category) {
                    Text("市场").tag("macro"); Text("板块").tag("sector"); Text("当前股票").tag("stock")
                }.pickerStyle(.segmented).padding(.horizontal)
                if category == "sector" {
                    HStack {
                        TextField("行业或板块", text: $sector).textFieldStyle(.roundedBorder).onSubmit { Task { await load(refresh: false) } }
                        Button("查看") { Task { await load(refresh: false) } }
                    }.padding(.horizontal)
                } else if category == "stock" {
                    Text(model.displayedStock.map { "\($0.name) · \($0.code)" } ?? "先从列表选择一只股票")
                        .font(.caption).foregroundStyle(.secondary)
                }
                List {
                    if let response {
                        Section {
                            HStack {
                                Text(response.status == "stale" ? "来源更新暂不可用，显示缓存" : response.status == "unavailable" ? "暂未取得新闻" : "来源新闻")
                                Spacer()
                                if let asOf = response.asOf { Text(ResearchStyle.date(asOf)) }
                            }.font(.caption2).foregroundStyle(.secondary)
                            ForEach(response.items) { item in newsRow(item) }
                        }
                    }
                    if let error { Text(error).font(.caption).foregroundStyle(.secondary) }
                    if loading { ProgressView("读取新闻…") }
                    if !loading && response?.items.isEmpty == true { Text("暂无此范围的新闻").foregroundStyle(.secondary) }
                }.listStyle(.plain).refreshable { await load(refresh: true) }
            }
            .navigationTitle("新闻").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .task(id: identity) { await load(refresh: false) }
            .onAppear { if sector.isEmpty { sector = model.displayedStock?.sector?.components(separatedBy: "/").first?.trimmingCharacters(in: .whitespaces) ?? "" } }
        }.presentationDetents([.large])
    }

    @ViewBuilder private func newsRow(_ item: StockNewsItem) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            if let url = ResearchStyle.safeURL(item.url) {
                Link(destination: url) { Text(item.title).font(.subheadline.weight(.medium)).foregroundStyle(AppStyle.ink) }
            } else { Text(item.title).font(.subheadline.weight(.medium)) }
            if let summary = item.summary, !summary.isEmpty { Text(summary).font(.caption).foregroundStyle(.secondary).lineLimit(4) }
            HStack(spacing: 6) {
                Text(item.source)
                if item.isLegacy { Text("历史摘要") }
                if let date = item.publishedAt { Text(ResearchStyle.date(date)) }
            }.font(.caption2).foregroundStyle(.secondary)
        }.padding(.vertical, 4)
    }

    private func load(refresh: Bool) async {
        let ticket = UUID(), scope = identity, kind = category, code = model.selectedCode, query = sector
        requestID = ticket; loading = true; error = nil
        if !refresh { response = nil }
        defer { if requestID == ticket { loading = false } }
        if (kind == "stock" && code == nil) || (kind == "sector" && query.trimmingCharacters(in: .whitespaces).isEmpty) { return }
        do {
            let value = try await model.client.news(category: kind, code: kind == "stock" ? code : nil,
                                                   sector: kind == "sector" ? query : nil, refresh: refresh)
            guard ticket == requestID, scope == identity, !Task.isCancelled else { return }
            response = value; error = value.warnings.first
        } catch { if ticket == requestID, scope == identity, !Task.isCancelled { self.error = error.localizedDescription } }
    }
}
