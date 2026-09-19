import Combine
import Foundation

enum WorkspaceCardKind: String, Codable, CaseIterable, Identifiable {
    case chart, kline, intraday, macd, kdj, fund, chipCosts, chipDistribution
    case klineChips, signals, peers, announcements, valuation, quote, orderBook, concepts

    var id: String { rawValue }

    var title: String {
        switch self {
        case .chart: "行情主图"
        case .kline: "K 线"
        case .intraday: "当日分时"
        case .macd: "MACD"
        case .kdj: "KDJ"
        case .fund: "资金流向"
        case .chipCosts: "筹码成本"
        case .chipDistribution: "筹码峰"
        case .klineChips: "K 线与筹码峰"
        case .signals: "市场信号"
        case .peers: "同行对比"
        case .announcements: "公司公告"
        case .valuation: "估值与业绩"
        case .quote: "行情指标"
        case .orderBook: "五档盘口"
        case .concepts: "所属概念"
        }
    }

    var description: String {
        switch self {
        case .chart: "在分时与 K 线间切换，拖动范围条查看走势。"
        case .kline: "独立显示蜡烛图、均线与成交量。"
        case .intraday: "查看当日价格、均价和成交量变化。"
        case .macd: "查看 DIF、DEA 和 MACD 柱线。"
        case .kdj: "查看 K、D、J 三条随机指标曲线。"
        case .fund: "查看主力及不同规模资金的流入流出。"
        case .chipCosts: "查看平均成本、获利比例和筹码集中度。"
        case .chipDistribution: "按价格观察筹码分布与密集区域。"
        case .klineChips: "将价格走势与筹码分布放在同一张卡片。"
        case .signals: "汇总已有市场信号和指标状态。"
        case .peers: "比较同业股票的行情与估值。"
        case .announcements: "查看公司公告及原文入口。"
        case .valuation: "查看估值、盈利和基本面指标。"
        case .quote: "查看开高低收、成交量和换手等行情指标。"
        case .orderBook: "查看买卖五档价格与挂单量。"
        case .concepts: "查看股票所属行业与概念。"
        }
    }

    var symbol: String {
        switch self {
        case .chart, .intraday: "chart.xyaxis.line"
        case .kline, .klineChips: "chart.bar.xaxis"
        case .macd, .kdj: "waveform.path"
        case .fund: "arrow.left.arrow.right"
        case .chipCosts, .chipDistribution: "chart.bar.fill"
        case .signals: "dot.radiowaves.left.and.right"
        case .peers: "building.2"
        case .announcements: "doc.text"
        case .valuation: "chart.pie"
        case .quote: "number"
        case .orderBook: "list.bullet.rectangle"
        case .concepts: "tag"
        }
    }

    var defaultSpan: WorkspaceCardSpan {
        switch self {
        case .chart, .kline, .intraday, .klineChips, .announcements: .full
        default: .half
        }
    }
}

enum WorkspaceCardSpan: String, Codable, CaseIterable, Identifiable {
    case half, full
    var id: String { rawValue }
    var title: String { self == .half ? "半宽" : "全宽" }
}

struct WorkspaceCard: Codable, Identifiable, Equatable {
    var kind: WorkspaceCardKind
    var span: WorkspaceCardSpan
    var isVisible: Bool
    var id: String { kind.rawValue }

    init(kind: WorkspaceCardKind, span: WorkspaceCardSpan? = nil, isVisible: Bool = true) {
        self.kind = kind
        self.span = span ?? kind.defaultSpan
        self.isVisible = isVisible
    }
}

struct WorkspacePage: Codable, Identifiable, Equatable {
    var id: String
    var title: String
    var cards: [WorkspaceCard]
    var visibleCards: [WorkspaceCard] { cards.filter(\.isVisible) }

    init(id: String = UUID().uuidString, title: String, cards: [WorkspaceCard] = []) {
        self.id = id
        self.title = title
        self.cards = cards
    }

    private enum CodingKeys: String, CodingKey { case id, title, cards }

    // A newer card kind must not make every saved page unreadable.
    private struct StoredCard: Decodable {
        let kind: String?
        let span: String?
        let isVisible: Bool?
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        title = try values.decodeIfPresent(String.self, forKey: .title) ?? "自定义"
        let stored = try values.decodeIfPresent([StoredCard].self, forKey: .cards) ?? []
        cards = stored.compactMap { card in
            guard let raw = card.kind, let kind = WorkspaceCardKind(rawValue: raw) else { return nil }
            return WorkspaceCard(
                kind: kind,
                span: card.span.flatMap(WorkspaceCardSpan.init(rawValue:)) ?? kind.defaultSpan,
                isVisible: card.isVisible ?? true
            )
        }
    }
}

struct WorkspaceLayout: Codable, Equatable {
    static let maximumPages = 8
    static let maximumCardsPerPage = 16
    static let maximumTitleLength = 24

    var schemaVersion: Int = 1
    var pages: [WorkspacePage]
    var selectedPageID: String?

    var selectedPage: WorkspacePage? {
        pages.first { $0.id == selectedPageID } ?? pages.first
    }

    static var defaultLayout: WorkspaceLayout {
        WorkspaceLayout(pages: [
            WorkspacePage(id: "chart", title: "走势", cards: [
                WorkspaceCard(kind: .chart, span: .full),
                WorkspaceCard(kind: .quote, span: .half),
                WorkspaceCard(kind: .orderBook, span: .half),
                WorkspaceCard(kind: .macd, span: .half),
                WorkspaceCard(kind: .kdj, span: .half),
                WorkspaceCard(kind: .fund, span: .half)
            ]),
            WorkspacePage(id: "research", title: "研究", cards: [
                WorkspaceCard(kind: .valuation, span: .half),
                WorkspaceCard(kind: .chipCosts, span: .half),
                WorkspaceCard(kind: .peers, span: .half),
                WorkspaceCard(kind: .concepts, span: .half)
            ]),
            WorkspacePage(id: "announcements", title: "公告", cards: [
                WorkspaceCard(kind: .announcements, span: .full)
            ])
        ], selectedPageID: "chart")
    }

    func sanitized() -> WorkspaceLayout {
        var pageIDs = Set<String>()
        var cleanedPages: [WorkspacePage] = []
        for source in pages.prefix(Self.maximumPages) {
            var page = source
            if page.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                page.id = UUID().uuidString
            }
            guard pageIDs.insert(page.id).inserted else { continue }
            page.title = String(page.title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(Self.maximumTitleLength))
            if page.title.isEmpty { page.title = "页面 \(cleanedPages.count + 1)" }
            var kinds = Set<WorkspaceCardKind>()
            page.cards = Array(page.cards.filter { kinds.insert($0.kind).inserted }.prefix(Self.maximumCardsPerPage))
            cleanedPages.append(page)
        }
        guard !cleanedPages.isEmpty else { return Self.defaultLayout }
        return WorkspaceLayout(
            schemaVersion: 1,
            pages: cleanedPages,
            selectedPageID: cleanedPages.contains { $0.id == selectedPageID } ? selectedPageID : cleanedPages[0].id
        )
    }
}

@MainActor
final class WorkspaceLayoutStore: ObservableObject {
    @Published private(set) var layout: WorkspaceLayout
    @Published private(set) var loadWarning: String?
    private let fileURL: URL

    init(fileURL: URL? = nil) {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support", isDirectory: true)
        self.fileURL = fileURL ?? support.appendingPathComponent("StocksNative", isDirectory: true)
            .appendingPathComponent("workspace-layout-v1.json")
        layout = .defaultLayout
        guard FileManager.default.fileExists(atPath: self.fileURL.path) else { return }
        do {
            let saved = try JSONDecoder().decode(WorkspaceLayout.self, from: Data(contentsOf: self.fileURL))
            guard saved.schemaVersion == 1 else {
                loadWarning = "已暂用默认布局，保存后会替换无法识别的布局版本。"
                return
            }
            layout = saved.sanitized()
        } catch {
            loadWarning = "原布局暂时无法读取，已使用默认布局；保存后会替换原布局。"
        }
    }

    func commit(_ draft: WorkspaceLayout) throws {
        let cleaned = draft.sanitized()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(cleaned)
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: fileURL, options: .atomic)
        layout = cleaned
        loadWarning = nil
    }

    func reset() throws { try commit(.defaultLayout) }

    func selectPage(_ id: String) throws {
        guard layout.pages.contains(where: { $0.id == id }), layout.selectedPageID != id else { return }
        var next = layout
        next.selectedPageID = id
        try commit(next)
    }
}
