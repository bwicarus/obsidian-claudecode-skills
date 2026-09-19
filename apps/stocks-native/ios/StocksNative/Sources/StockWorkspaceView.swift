import SwiftUI

struct StockWorkspaceView: View {
    @ObservedObject var model: AppModel
    let detail: StockResponse
    @ObservedObject private var workspace: WorkspaceLayoutStore
    @State private var showingEditor = false
    @AppStorage("stocksNative.workspaceLocked") private var layoutLocked = false
    @State private var layoutError: String?

    init(model: AppModel, detail: StockResponse) {
        self.model = model
        self.detail = detail
        self.workspace = model.workspace
    }

    private var page: WorkspacePage? { workspace.layout.selectedPage }
    private var stock: Stock { model.displayedStock ?? detail.stock }

    var body: some View {
        VStack(spacing: 0) {
            if let page, !page.visibleCards.isEmpty {
                NativeWorkspaceCanvas(page: page, isEditing: !layoutLocked,
                                      content: canvasCard, onCommit: saveCards)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ContentUnavailableView {
                    Label("这个页签还没有显示的卡片", systemImage: "rectangle.grid.2x2")
                } description: {
                    Text("从卡片库添加行情、指标或资料，再直接拖动卡片排布。")
                } actions: {
                    Button("添加卡片") { showingEditor = true }.buttonStyle(.bordered)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            pageDock
        }
        .sheet(isPresented: $showingEditor) {
            WorkspaceEditor(store: workspace, onApply: model.workspaceDidChange)
                .presentationDetents([.large])
                .presentationDragIndicator(.visible)
        }
        .onAppear { model.workspaceDidChange() }
        .alert("布局未保存", isPresented: Binding(get: { layoutError != nil }, set: { if !$0 { layoutError = nil } })) {
            Button("好", role: .cancel) { layoutError = nil }
        } message: { Text(layoutError ?? "请重试。") }
    }

    private var pageDock: some View {
        HStack(spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(workspace.layout.pages) { item in
                        Button { model.selectWorkspacePage(item.id) } label: {
                            Text(item.title)
                                .font(.subheadline.weight(.medium))
                                .lineLimit(1).padding(.horizontal, 18).padding(.vertical, 11)
                                .frame(minHeight: 44)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(item.id == page?.id ? AppStyle.accent : Color.secondary)
                        .background(item.id == page?.id ? AppStyle.accent.opacity(0.10) : Color.clear,
                                    in: RoundedRectangle(cornerRadius: 10))
                        .accessibilityAddTraits(item.id == page?.id ? .isSelected : [])
                    }
                }
            }
            Button { layoutLocked.toggle() } label: {
                Image(systemName: layoutLocked ? "lock.fill" : "lock.open")
                    .frame(width: 44, height: 44)
                    .background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 10))
            }
            .buttonStyle(.plain).foregroundStyle(AppStyle.accent)
            .accessibilityLabel(layoutLocked ? "解锁卡片布局" : "锁定卡片布局")
            .help(layoutLocked ? "解锁后可拖动卡片、角标和共享边" : "锁定后只操作图表")
            Button { showingEditor = true } label: {
                Image(systemName: "slider.horizontal.3")
                    .font(.subheadline.weight(.medium))
                    .frame(width: 44, height: 44)
                    .background(AppStyle.canvas, in: RoundedRectangle(cornerRadius: 10))
            }
            .buttonStyle(.plain).foregroundStyle(AppStyle.accent)
            .accessibilityLabel("页签与卡片库")
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.white)
        .overlay(alignment: .top) { Divider() }
    }

    @ViewBuilder
    private func workspaceCard(_ kind: WorkspaceCardKind) -> some View {
        switch kind {
        case .chart:
            MarketChartSection(model: model, stockCode: stock.code)
        case .kline:
            MarketChartSection(model: model, stockCode: stock.code, mode: .kline)
        case .intraday:
            MarketChartSection(model: model, stockCode: stock.code, mode: .intraday)
        case .macd:
            TechnicalCard(panel: detail.technical, candles: model.displayedKlineCandles,
                          visibleContext: model.chartSnapshotForKline, period: model.klinePeriod)
        case .kdj:
            KDJCard(panel: detail.technical, candles: model.displayedKlineCandles,
                    visibleContext: model.chartSnapshotForKline, period: model.klinePeriod)
        case .fund:
            if let fund = detail.fund { FundCard(panel: fund) }
            else { unavailableCard(kind) }
        case .chipCosts:
            if let chips = detail.chips { ChipCard(panel: chips) }
            else { unavailableCard(kind) }
        case .chipDistribution:
            ChipDistributionCard(data: model.chipDistribution, currentPrice: stock.price)
        case .klineChips:
            MarketChartSection(model: model, stockCode: stock.code, mode: .withChips)
        case .signals:
            WorkspaceSignalsCard(signals: detail.signals)
        case .peers:
            if let peers = detail.peers, !peers.isEmpty { PeersCard(peers: peers, model: model) }
            else { unavailableCard(kind) }
        case .announcements:
            StockAnnouncementsSection(detail: detail)
        case .valuation:
            StockValuationCard(detail: detail)
        case .quote:
            WorkspaceQuoteCard(stock: stock)
        case .orderBook:
            OrderBookPanel(stock: stock, compact: true)
        case .concepts:
            if let concepts = detail.concepts, !concepts.isEmpty { ConceptsCard(concepts: concepts) }
            else { unavailableCard(kind) }
        }
    }

    private func unavailableCard(_ kind: WorkspaceCardKind) -> some View {
        WorkspaceCardSurface(title: kind.title) {
            Label("暂时没有这只股票的相关数据", systemImage: kind.symbol)
                .font(.subheadline).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 110)
        }
    }

    private func refresh() async {
        await model.loadDetail()
        await model.loadRealtime()
        await model.loadChart()
    }

    private func canvasCard(_ card: WorkspaceCard) -> AnyView {
        AnyView(GeometryReader { geometry in
            Group {
                switch card.kind {
                case .chart, .kline, .intraday, .klineChips:
                    // Chart cards own their scrollable body and pinned range controls.
                    workspaceCard(card.kind)
                default:
                    ScrollView(.vertical) {
                        workspaceCard(card.kind)
                            .frame(maxWidth: .infinity, minHeight: geometry.size.height, alignment: .topLeading)
                    }
                    .scrollBounceBehavior(.basedOnSize)
                }
            }
            .environment(\.workspaceCardHeight, geometry.size.height)
            .environment(\.workspaceCardWidth, geometry.size.width)
        })
    }

    private func saveCards(_ cards: [WorkspaceCard]) {
        guard let id = page?.id else { return }
        var next = workspace.layout
        guard let index = next.pages.firstIndex(where: { $0.id == id }) else { return }
        next.pages[index].cards = cards
        do {
            try workspace.commit(next)
            model.workspaceDidChange()
        } catch { layoutError = "无法保存本机布局：\(error.localizedDescription)" }
    }
}

private struct WorkspaceCardHeightKey: EnvironmentKey {
    static let defaultValue: CGFloat = 0
}

private struct WorkspaceCardWidthKey: EnvironmentKey {
    static let defaultValue: CGFloat = 0
}

extension EnvironmentValues {
    var workspaceCardHeight: CGFloat {
        get { self[WorkspaceCardHeightKey.self] }
        set { self[WorkspaceCardHeightKey.self] = newValue }
    }

    var workspaceCardWidth: CGFloat {
        get { self[WorkspaceCardWidthKey.self] }
        set { self[WorkspaceCardWidthKey.self] = newValue }
    }
}

private struct WorkspaceCardSurface<Content: View>: View {
    let title: String
    private let content: Content

    init(title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(title).font(.headline).foregroundStyle(AppStyle.ink)
            content
        }
        .padding(20).frame(maxWidth: .infinity, alignment: .leading)
        .background(.white, in: RoundedRectangle(cornerRadius: 20))
    }
}

private struct WorkspaceQuoteCard: View {
    let stock: Stock

    var body: some View {
        WorkspaceCardSurface(title: "行情指标") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 82), spacing: 12, alignment: .leading)],
                      alignment: .leading, spacing: 16) {
                metric("今开", AppStyle.price(stock.open), color: priceColor(stock.open))
                metric("最高", AppStyle.price(stock.high), color: priceColor(stock.high))
                metric("最低", AppStyle.price(stock.low), color: priceColor(stock.low))
                metric("昨收", AppStyle.price(stock.prevClose))
                metric("成交量", AppStyle.compact(stock.volume))
                metric("成交额", AppStyle.compact(stock.turnover))
                metric("换手率", AppStyle.percent(stock.turnoverRate))
                metric("量比", stock.volumeRatio.map { String(format: "%.2f", $0) } ?? "—")
                metric("振幅", AppStyle.percent(stock.amplitude))
            }
        }
    }

    private func metric(_ title: String, _ value: String, color: Color = AppStyle.ink) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.medium)).monospacedDigit()
                .foregroundStyle(color).lineLimit(1).minimumScaleFactor(0.8)
        }
    }

    private func priceColor(_ value: Double?) -> Color {
        guard let value, let previous = stock.prevClose else { return AppStyle.ink }
        return AppStyle.movement(value - previous)
    }
}

private struct WorkspaceSignalsCard: View {
    let signals: StockSignals?

    var body: some View {
        WorkspaceCardSurface(title: "市场信号") {
            signalGroup("龙虎榜", rows: signals?.topList ?? []) { item in
                if let reason = item.reason, !reason.isEmpty {
                    Text(reason).font(.caption).foregroundStyle(AppStyle.ink)
                }
                value("净买入", item.netAmount, compact: true)
                if item.netRate != nil { value("净买入占比", item.netRate, suffix: "%") }
            }
            Divider()
            signalGroup("北向交易", rows: signals?.northbound ?? []) { item in
                if let rank = item.rank { textValue("成交排名", "\(rank)") }
                value("成交额", item.amount, compact: true)
                if item.buy != nil { value("买入额", item.buy, compact: true) }
                if item.sell != nil { value("卖出额", item.sell, compact: true) }
            }
            Divider()
            signalGroup("涨跌停价格", rows: Array((signals?.limits ?? []).prefix(1))) { item in
                value("涨停价", item.upLimit)
                value("跌停价", item.downLimit)
            }
        }
    }

    private func signalGroup<Content: View>(_ title: String, rows: [StockSignal],
                                            @ViewBuilder content: @escaping (StockSignal) -> Content) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            if rows.isEmpty { Text("暂无记录").font(.caption).foregroundStyle(.tertiary) }
            else {
                ForEach(Array(rows.prefix(3).enumerated()), id: \.offset) { index, item in
                    if index > 0 { Divider() }
                    if let date = item.tradeDate { Text(date).font(.caption2).monospaced().foregroundStyle(.secondary) }
                    content(item)
                }
            }
        }
    }

    private func value(_ title: String, _ amount: Double?, compact: Bool = false, suffix: String = "") -> some View {
        textValue(title, amount.map { (compact ? AppStyle.compact($0) : String(format: "%.2f", $0)) + suffix } ?? "—")
    }

    private func textValue(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title).foregroundStyle(.secondary)
            Spacer()
            Text(value).foregroundStyle(AppStyle.ink).monospacedDigit()
        }
        .font(.caption)
    }
}
