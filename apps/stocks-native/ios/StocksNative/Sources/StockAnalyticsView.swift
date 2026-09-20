import Charts
import SwiftUI

struct QuoteMetricGrid: View {
    let stock: Stock

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 0) {
                metric("今开", AppStyle.price(stock.open))
                metric("最高", AppStyle.price(stock.high), color: quoteColor(stock.high))
                metric("最低", AppStyle.price(stock.low), color: quoteColor(stock.low))
                metric("昨收", AppStyle.price(stock.prevClose))
                metric("成交量", AppStyle.compact(stock.volume))
                metric("成交额", AppStyle.compact(stock.turnover))
                metric("换手", AppStyle.percent(stock.turnoverRate))
                metric("量比", stock.volumeRatio.map { String(format: "%.2f", $0) } ?? "—")
                metric("振幅", AppStyle.percent(stock.amplitude))
            }
            .padding(.horizontal, 14)
        }
        .background(.white)
    }

    private func metric(_ title: String, _ value: String, color: Color = AppStyle.ink) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.system(.subheadline, design: .rounded, weight: .medium))
                .monospacedDigit().foregroundStyle(color)
        }
        .padding(.horizontal, 10).padding(.vertical, 10)
        .frame(minWidth: 82, alignment: .leading)
        .overlay(alignment: .trailing) {
            Rectangle().fill(Color.secondary.opacity(0.14)).frame(width: 0.5, height: 28)
        }
    }

    private func quoteColor(_ value: Double?) -> Color {
        guard let value, let previous = stock.prevClose else { return AppStyle.ink }
        return AppStyle.movement(value - previous)
    }
}

struct StockAssistantInspector: View {
    @ObservedObject var model: AppModel
    let onClose: () -> Void

    var body: some View {
        VoiceSidebar(voice: model.voice, model: model, onClose: onClose)
            .background(.white)
    }
}

struct StockMarketWorkspace: View {
    @ObservedObject var model: AppModel
    let detail: StockResponse
    let availableWidth: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if availableWidth >= 850 {
                HStack(alignment: .top, spacing: 16) {
                    MarketChartSection(model: model, stockCode: detail.stock.code)
                        .frame(maxWidth: .infinity)
                    OrderBookPanel(stock: model.displayedStock ?? detail.stock, compact: false)
                        .frame(width: 230)
                }
            } else {
                MarketChartSection(model: model, stockCode: detail.stock.code)
                OrderBookPanel(stock: model.displayedStock ?? detail.stock, compact: true)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 290), spacing: 16, alignment: .top)],
                      alignment: .leading, spacing: 16) {
                if let technical = detail.technical {
                    TechnicalCard(panel: technical)
                    KDJCard(panel: technical)
                }
                if let fund = detail.fund { FundCard(panel: fund) }
            }
        }
    }
}

struct OrderBookPanel: View {
    @Environment(\.workspaceCardWidth) private var availableWidth
    let stock: Stock
    let compact: Bool
    private static let columnBreakpoint: CGFloat = 420
    private static let contentPadding: CGFloat = 12
    private static let sectionSpacing: CGFloat = 6
    private static let rowSpacing: CGFloat = 3
    private var showsColumns: Bool { availableWidth > 0 ? availableWidth >= Self.columnBreakpoint : compact }

    static func minimumContentHeight(stock: Stock, width: CGFloat) -> CGFloat {
        let bids = min(stock.bids?.count ?? 0, 5), asks = min(stock.asks?.count ?? 0, 5)
        func sectionHeight(_ count: Int) -> CGFloat { CGFloat(count + 1) * 16 + CGFloat(count) * rowSpacing }
        var sections: [CGFloat] = [22]
        if bids == 0 && asks == 0 { sections.append(20) }
        else if width >= columnBreakpoint { sections.append(max(sectionHeight(bids), sectionHeight(asks))) }
        else { sections += [sectionHeight(asks), 1, sectionHeight(bids)] }
        if stock.innerVolume != nil && stock.outerVolume != nil { sections += [1, 16] }
        return max(80, contentPadding * 2 + sections.reduce(0, +) + CGFloat(sections.count - 1) * sectionSpacing)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Self.sectionSpacing) {
            Text("五档盘口").font(.headline)
            if (stock.asks ?? []).isEmpty && (stock.bids ?? []).isEmpty {
                Text("暂无五档数据").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else if showsColumns {
                HStack(alignment: .top, spacing: 16) {
                    bookSection("买盘", rows: stock.bids ?? [], tint: AppStyle.up)
                    bookSection("卖盘", rows: stock.asks ?? [], tint: AppStyle.down)
                }
            } else {
                bookSection("卖盘", rows: Array((stock.asks ?? []).prefix(5).reversed()), tint: AppStyle.down, reversed: true)
                Divider()
                bookSection("买盘", rows: stock.bids ?? [], tint: AppStyle.up)
            }
            if let inner = stock.innerVolume, let outer = stock.outerVolume {
                Divider()
                HStack {
                    smallValue("内盘", AppStyle.compact(inner))
                    Spacer()
                    smallValue("外盘", AppStyle.compact(outer))
                }
            }
        }
        .padding(Self.contentPadding)
        .fixedSize(horizontal: false, vertical: true)
        .background(.white, in: RoundedRectangle(cornerRadius: 18))
    }

    private func bookSection(_ title: String, rows: [OrderLevel], tint: Color, reversed: Bool = false) -> some View {
        VStack(spacing: Self.rowSpacing) {
            HStack {
                Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Text("价格 / 量").font(.caption2).foregroundStyle(.tertiary)
            }
            ForEach(Array(rows.prefix(5).enumerated()), id: \.offset) { index, row in
                HStack(spacing: 5) {
                    Text("\(reversed ? min(rows.count, 5) - index : index + 1)")
                        .foregroundStyle(.tertiary)
                    Spacer(minLength: 2)
                    Text(AppStyle.price(row.price)).foregroundStyle(tint)
                    Text(AppStyle.compact(row.volume)).foregroundStyle(.secondary)
                        .frame(minWidth: 42, alignment: .trailing)
                }
                .font(.caption.monospacedDigit()).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func smallValue(_ title: String, _ value: String) -> some View {
        HStack(spacing: 4) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit()).foregroundStyle(AppStyle.ink)
        }
    }
}

struct StockValuationCard: View {
    @Environment(\.workspaceCardWidth) private var availableWidth
    let detail: StockResponse

    var body: some View {
        card(title: "估值与表现", subtitle: StockChartLabels.detail(detail.asOf)) {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), alignment: .leading),
                                    count: availableWidth >= 500 ? 2 : 1),
                      alignment: .leading, spacing: 8) {
            valueLine("市盈率", detail.stock.peDynamic, color: AppStyle.ink)
            valueLine("市净率", detail.stock.pb, color: AppStyle.ink)
            valueLine("总市值", detail.stock.marketCap, compact: true, color: AppStyle.ink)
            valueLine("流通市值", detail.stock.floatMarketCap, compact: true, color: AppStyle.ink)
            valueLine("60 日涨跌", detail.stock.change60d, suffix: "%")
            valueLine("年内涨跌", detail.stock.changeYtd, suffix: "%")
            if let ratio = detail.technical?.metrics.profitRatio {
                valueLine("技术获利盘", abs(ratio) <= 1 ? ratio * 100 : ratio, suffix: "%", color: AppStyle.ink)
            }
            }
        }
    }
}

enum WorkspaceChartMode: Hashable { case adaptive, kline, intraday, withChips }

struct MarketChartSection: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    @ObservedObject var model: AppModel
    let stockCode: String
    var mode: WorkspaceChartMode = .adaptive

    private var isIntraday: Bool { mode == .intraday || (mode == .adaptive && model.chartPeriod == .intraday) }
    private var candlePeriod: ChartPeriod { model.klinePeriod }
    private var candles: [Candle] { model.displayedKlineCandles }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let error = model.chartError {
                Label(error, systemImage: "wifi.exclamationmark")
                    .font(.caption).foregroundStyle(.red)
            }
            if availableHeight > 0 {
                GeometryReader { geometry in
                    chartContent.environment(\.workspaceCardHeight, geometry.size.height)
                }
            } else {
                chartContent
            }
        }
    }

    @ViewBuilder private var chartContent: some View {
        if isIntraday {
            if let intraday = model.displayedIntraday, !intraday.rows.isEmpty {
                IntradayChart(data: intraday, stockCode: stockCode, annotations: model.annotations,
                              onContextChange: { snapshot in
                                  await model.updateChartContext(snapshot, sourceID: "\(mode):intraday")
                              }, sharedRange: model.linkedIntradayRange, contextSuppressed: model.isTimelineEditing)
                    .id(stockCode)
            } else if model.isLoadingChart {
                chartLoading
            } else {
                emptyChart("当天暂无分时数据")
            }
        } else if !candles.isEmpty {
            CandleChart(candles: candles, stockCode: stockCode, period: candlePeriod,
                        annotations: model.annotations, onContextChange: { snapshot in
                            await model.updateChartContext(snapshot, sourceID: "\(mode):kline")
                        },
                        chipDistribution: model.chipDistribution, showsChips: mode == .withChips,
                        externalContext: model.linkedKlineContext, sharedRange: model.linkedKlineRange,
                        contextSuppressed: model.isTimelineEditing, currentPrice: model.displayedStock?.price)
                .id("\(stockCode):\(candlePeriod.rawValue):\(mode)")
        } else if model.isLoadingChart {
            chartLoading
        } else {
            emptyChart("暂无这个周期的 K 线")
        }
    }

    private var chartLoading: some View {
        ProgressView("更新行情…")
            .frame(maxWidth: .infinity, minHeight: 300)
            .background(.white, in: RoundedRectangle(cornerRadius: 22))
    }

    private func emptyChart(_ title: String) -> some View {
        ContentUnavailableView(title, systemImage: "chart.xyaxis.line")
            .frame(maxWidth: .infinity, minHeight: 300)
            .background(.white, in: RoundedRectangle(cornerRadius: 22))
    }
}

private struct SessionIntradayPoint: Identifiable {
    let point: IntradayPoint
    let minute: Double
    var id: String { point.time }

    init?(_ point: IntradayPoint) {
        let parts = point.time.split(separator: ":")
        guard parts.count == 2, let hour = Int(parts[0]), let minute = Int(parts[1]) else { return nil }
        let clock = hour * 60 + minute
        if (570...690).contains(clock) {
            self.minute = Double(clock - 570)
        } else if (780...900).contains(clock) {
            // The opening and closing observations around lunch get distinct slots.
            self.minute = Double(clock - 780 + 121)
        } else {
            return nil
        }
        self.point = point
    }
}

struct IntradayChart: View {
    let data: IntradayResponse
    let stockCode: String
    @ObservedObject var annotations: AnnotationStore
    let onContextChange: (VoiceChartSnapshot) async -> Void
    @State private var selectedTime: String?
    var sharedRange: Range<Int>? = nil
    var contextSuppressed = false
    private var window: Range<Int> { sharedRange ?? 0..<242 }

    private var points: [SessionIntradayPoint] {
        data.rows.compactMap(SessionIntradayPoint.init).sorted { $0.minute < $1.minute }
    }
    private var visiblePoints: [SessionIntradayPoint] {
        points.filter { window.contains(Int($0.minute)) }
    }
    private var xDomain: ClosedRange<Double> {
        Double(window.lowerBound)...Double(max(window.lowerBound + 1, window.upperBound - 1))
    }
    private var tickPositions: [Double] {
        window == 0..<242 ? [0, 120.5, 241] : [Double(window.lowerBound), Double((window.lowerBound + window.upperBound - 1) / 2), Double(window.upperBound - 1)]
    }
    private func sessionTime(_ slot: Int) -> String {
        let clock = slot <= 120 ? 570 + slot : 780 + slot - 121
        return String(format: "%02d:%02d", clock / 60, clock % 60)
    }
    private var annotationViewport: ChartAnnotationViewport {
        ChartAnnotationViewport(period: ChartPeriod.intraday.rawValue,
                                times: (0..<242).map { "\(data.tradeDate)T\(sessionTime($0))" },
                                xDomain: xDomain, yDomain: domain)
    }
    private var selectedPoint: SessionIntradayPoint? {
        guard let selectedTime else { return nil }
        return visiblePoints.first { $0.point.time == selectedTime }
    }
    private var inspected: SessionIntradayPoint? { selectedPoint ?? visiblePoints.last }
    private var selection: Binding<Double?> {
        Binding(get: { selectedPoint?.minute }, set: { value in
            guard let value, value.isFinite else { selectedTime = nil; return }
            selectedTime = visiblePoints.min { abs($0.minute - value) < abs($1.minute - value) }?.point.time
        })
    }

    private var voiceContextSnapshot: VoiceChartSnapshot {
        let point = inspected.map { VoiceChartPoint(time: $0.point.time, price: $0.point.price, volume: $0.point.volume) }
        let chart = VoiceChartContext(stockCode: stockCode, period: ChartPeriod.intraday.rawValue,
                                      kind: "intraday", firstVisibleTime: visiblePoints.first?.point.time,
                                      lastVisibleTime: visiblePoints.last?.point.time,
                                      visiblePointCount: visiblePoints.count, selectedPoint: point,
                                      selectionSource: selectedPoint == nil ? (visiblePoints.last?.id == points.last?.id ? "latest" : "visible_end") : "cursor")
        return VoiceChartSnapshot(chart: chart,
                                  annotations: annotations.voiceContext(stockCode: stockCode, editing: false, tool: .pen,
                                                                        viewport: annotationViewport))
    }

    private var domain: ClosedRange<Double> {
        let values = visiblePoints.flatMap { [$0.point.price, $0.point.averagePrice].compactMap { $0 } }
        guard !values.isEmpty else {
            let reference = data.previousClose ?? points.last?.point.price ?? 1
            let padding = max(abs(reference) * 0.005, 0.01)
            return (reference - padding)...(reference + padding)
        }
        let low = values.min() ?? 0
        let high = values.max() ?? 1
        if window != 0..<242 {
            let padding = max((high - low) * 0.10, abs(high) * 0.001, 0.01)
            return (low - padding)...(high + padding)
        }
        let reference = data.previousClose ?? (low + high) / 2
        let distance = max(abs(high - reference), abs(reference - low), reference * 0.005, 0.01)
        return (reference - distance * 1.08)...(reference + distance * 1.08)
    }

    var body: some View {
        ChartCardViewport { height in
            chartContent(contentHeight: height)
        }
        .task(id: contextSuppressed ? nil : voiceContextSnapshot) {
            if !contextSuppressed { await onContextChange(voiceContextSnapshot) }
        }
        .onChange(of: data.tradeDate) { _, _ in selectedTime = nil }
        .onChange(of: window) { _, _ in selectedTime = nil }
        .onChange(of: visiblePoints.map(\.id)) { _, times in
            if let selectedTime, !times.contains(selectedTime) { self.selectedTime = nil }
        }
    }

    private func chartContent(contentHeight: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("当日分时").font(.headline)
                    Text("\(StockChartLabels.day(data.tradeDate)) · 常规交易时段")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let latest = inspected?.point {
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(AppStyle.price(latest.price)).font(.title3.weight(.semibold)).monospacedDigit()
                        Text(StockChartLabels.detail(latest.time, tradeDate: data.tradeDate))
                            .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                    }
                }
            }
            ZStack {
                Chart {
                    if let previous = data.previousClose, domain.contains(previous) {
                        RuleMark(y: .value("昨收", previous))
                            .foregroundStyle(.secondary.opacity(0.35))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    }
                    ForEach(visiblePoints) { item in
                        LineMark(x: .value("交易分钟", item.minute), y: .value("价格", item.point.price),
                                 series: .value("曲线", "价格"))
                            .foregroundStyle(by: .value("曲线", "价格"))
                            .lineStyle(StrokeStyle(lineWidth: 2))
                        AreaMark(x: .value("交易分钟", item.minute), yStart: .value("下沿", domain.lowerBound),
                                 yEnd: .value("价格", item.point.price))
                            .foregroundStyle(LinearGradient(colors: [AppStyle.accent.opacity(0.18), .clear],
                                                            startPoint: .top, endPoint: .bottom))
                        if let average = item.point.averagePrice {
                            LineMark(x: .value("交易分钟", item.minute), y: .value("均价", average),
                                     series: .value("曲线", "均价"))
                                .foregroundStyle(by: .value("曲线", "均价"))
                                .lineStyle(StrokeStyle(lineWidth: 1.2))
                        }
                    }
                    if visiblePoints.count == 1, selectedPoint == nil, let only = visiblePoints.first {
                        PointMark(x: .value("交易分钟", only.minute), y: .value("价格", only.point.price))
                            .foregroundStyle(AppStyle.accent).symbolSize(25)
                    }
                    if let selectedPoint {
                        RuleMark(x: .value("选中", selectedPoint.minute))
                            .foregroundStyle(AppStyle.ink.opacity(0.3))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                        PointMark(x: .value("选中", selectedPoint.minute), y: .value("价格", selectedPoint.point.price))
                            .foregroundStyle(AppStyle.accent).symbolSize(25)
                    }
                }
                .chartXScale(domain: xDomain, range: .plotDimension(padding: 0))
                .chartYScale(domain: domain)
                .chartXSelection(value: selection)
                .chartForegroundStyleScale(["价格": AppStyle.accent, "均价": Color.orange.opacity(0.85)])
                .chartLegend(.hidden)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 5)) { value in
                        AxisGridLine().foregroundStyle(.gray.opacity(0.12))
                        AxisValueLabel {
                            if let number = value.as(Double.self) {
                                Text(AppStyle.price(number)).font(.caption2).monospacedDigit()
                                    .frame(width: 52, alignment: .trailing)
                            }
                        }
                    }
                }
                .chartXAxis {
                    AxisMarks(values: tickPositions) { value in
                        AxisValueLabel {
                            if let minute = value.as(Double.self) {
                                Text(minute == 120.5 ? "11:30 / 13:00" : sessionTime(Int(minute)))
                                    .font(.caption2)
                            }
                        }
                    }
                }
                .chartOverlay { proxy in
                    GeometryReader { geometry in
                        if let plotFrame = proxy.plotFrame {
                            let frame = geometry[plotFrame]
                            NativeAnnotationCanvas(store: annotations, stockCode: stockCode,
                                                   tool: .pen, color: "accent", isEditing: false,
                                                   viewport: annotationViewport)
                                .frame(width: frame.width, height: frame.height)
                                .clipped()
                                .offset(x: frame.minX, y: frame.minY)
                        }
                    }
                    .allowsHitTesting(false)
                }
                if visiblePoints.isEmpty {
                    ContentUnavailableView("当前区间暂无分时数据", systemImage: "chart.xyaxis.line")
                        .background(.white)
                        .allowsHitTesting(false)
                }
            }
            .frame(height: contentHeight > 0 ? max(110, contentHeight - 132) : 200)
            HStack(spacing: 18) {
                Label("价格", systemImage: "minus").foregroundStyle(AppStyle.accent)
                Label("均价", systemImage: "minus").foregroundStyle(.orange)
                Spacer()
                if let previous = data.previousClose {
                    Text("昨收 \(AppStyle.price(previous))").foregroundStyle(.secondary)
                }
            }
            .font(.caption)
            Chart(visiblePoints) { item in
                BarMark(x: .value("交易分钟", item.minute), y: .value("成交量", item.point.volume), width: .ratio(0.8))
                    .foregroundStyle(AppStyle.accent.opacity(0.38))
            }
            .chartXScale(domain: xDomain, range: .plotDimension(padding: 0))
            .chartXAxis(.hidden)
            .chartYAxis {
                AxisMarks(position: .trailing, values: .automatic(desiredCount: 2)) { value in
                    AxisValueLabel {
                        if let number = value.as(Double.self) {
                            Text(number.formatted(.number.notation(.compactName)))
                                .font(.caption2).monospacedDigit()
                                .frame(width: 52, alignment: .trailing)
                        }
                    }
                }
            }
            .frame(height: 48)
        }
    }


}

struct StockAnalyticsSections: View {
    @ObservedObject var model: AppModel
    let detail: StockResponse
    private let columns = [GridItem(.adaptive(minimum: 310), spacing: 18, alignment: .top)]

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 18) {
            StockValuationCard(detail: detail)
            if let chips = detail.chips { ChipCard(panel: chips) }
            if let peers = detail.peers, !peers.isEmpty { PeersCard(peers: peers, model: model) }
            if let concepts = detail.concepts, !concepts.isEmpty { ConceptsCard(concepts: concepts) }
        }
    }
}

struct StockAnnouncementsSection: View {
    let detail: StockResponse

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .firstTextBaseline) {
                Text("公司公告").font(.title3.weight(.semibold)).foregroundStyle(AppStyle.ink)
                Spacer()
                Text(detail.stock.code).font(.caption).monospaced().foregroundStyle(.secondary)
            }
            if let announcements = detail.announcements, !announcements.isEmpty {
                AnnouncementsCard(items: announcements)
            } else {
                ContentUnavailableView("暂无公告", systemImage: "doc.text.magnifyingglass",
                                       description: Text("服务器尚未返回这只股票的近期披露。"))
                    .frame(maxWidth: .infinity, minHeight: 280)
            }
        }
    }
}

struct TechnicalCard: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    var panel: TechnicalPanel? = nil
    var candles: [Candle] = []
    var visibleContext: VoiceChartContext? = nil
    var period: ChartPeriod = .day

    private var points: [NativeIndicatorPoint] {
        NativeChartIndicators.visible(NativeChartIndicators.series(candles: candles, panel: period == .day ? panel : nil), context: visibleContext)
    }
    private var inspected: NativeIndicatorPoint? { NativeChartIndicators.inspected(points, context: visibleContext) }
    private var xDomain: ClosedRange<Double> { indicatorDomain(points) }
    private var scale: NativeIndicatorScale {
        NativeIndicatorScale.macd(points.flatMap { [$0.dif, $0.dea, $0.histogram].compactMap { $0 } })
    }

    var body: some View {
        card(title: "MACD", subtitle: "\(period.title) · 12 / 26 / 9") {
            if points.isEmpty { indicatorEmpty }
            else {
                HStack(spacing: 12) {
                    indicatorValue("DIF", inspected?.dif, color: AppStyle.accent, scale: scale)
                    indicatorValue("DEA", inspected?.dea, color: .orange, scale: scale)
                    indicatorValue("MACD", inspected?.histogram, color: AppStyle.movement(inspected?.histogram), scale: scale)
                }
                Chart {
                    RuleMark(y: .value("零轴", 0.0)).foregroundStyle(.secondary.opacity(0.3))
                    ForEach(points) { point in
                        if let histogram = point.histogram, histogram.isFinite {
                            BarMark(x: .value("时间", Double(point.id)), y: .value("MACD", histogram), width: .ratio(0.6))
                                .foregroundStyle(AppStyle.movement(histogram).opacity(0.55))
                        }
                        if let dif = point.dif, dif.isFinite {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("DIF", dif), series: .value("指标", "DIF"))
                                .foregroundStyle(AppStyle.accent).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if let dea = point.dea, dea.isFinite {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("DEA", dea), series: .value("指标", "DEA"))
                                .foregroundStyle(.orange).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if point.time == inspected?.time {
                            RuleMark(x: .value("查看", Double(point.id)))
                                .foregroundStyle(.secondary.opacity(0.2)).lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
                        }
                    }
                }
                .chartXScale(domain: xDomain, range: .plotDimension(padding: 0))
                .chartYScale(domain: scale.domain)
                .chartXAxis { indicatorTimeAxis(points) }
                .chartYAxis { indicatorAxis(scale) }.chartLegend(.hidden)
                .frame(height: availableHeight > 0 ? max(100, availableHeight - 118) : 130)
                indicatorDate(inspected?.time)
            }
        }
    }
}

struct KDJCard: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    var panel: TechnicalPanel? = nil
    var candles: [Candle] = []
    var visibleContext: VoiceChartContext? = nil
    var period: ChartPeriod = .day

    private var points: [NativeIndicatorPoint] {
        NativeChartIndicators.visible(NativeChartIndicators.series(candles: candles, panel: period == .day ? panel : nil), context: visibleContext)
    }
    private var inspected: NativeIndicatorPoint? { NativeChartIndicators.inspected(points, context: visibleContext) }
    private var scale: NativeIndicatorScale {
        NativeIndicatorScale.kdj(points.flatMap { [$0.k, $0.d, $0.j].compactMap { $0 } })
    }

    var body: some View {
        card(title: "KDJ", subtitle: "\(period.title) · 9 / 3 / 3") {
            if points.isEmpty { indicatorEmpty }
            else {
                HStack(spacing: 18) {
                    indicatorValue("K", inspected?.k, color: AppStyle.accent, scale: scale)
                    indicatorValue("D", inspected?.d, color: .orange, scale: scale)
                    indicatorValue("J", inspected?.j, color: .purple, scale: scale)
                }
                Chart {
                    ForEach([20.0, 80.0], id: \.self) { value in
                        RuleMark(y: .value("参考", value)).foregroundStyle(.secondary.opacity(0.3))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    }
                    ForEach(points) { point in
                        if let k = point.k, k.isFinite {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("K", k), series: .value("指标", "K"))
                                .foregroundStyle(AppStyle.accent).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if let d = point.d, d.isFinite {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("D", d), series: .value("指标", "D"))
                                .foregroundStyle(.orange).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if let j = point.j, j.isFinite {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("J", j), series: .value("指标", "J"))
                                .foregroundStyle(.purple).lineStyle(StrokeStyle(lineWidth: 1.2))
                        }
                    }
                    if let inspected {
                        RuleMark(x: .value("查看", Double(inspected.id))).foregroundStyle(.secondary.opacity(0.2))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
                    }
                }
                .chartXScale(domain: indicatorDomain(points), range: .plotDimension(padding: 0))
                .chartYScale(domain: scale.domain)
                .chartXAxis { indicatorTimeAxis(points) }
                .chartYAxis { indicatorAxis(scale) }.chartLegend(.hidden)
                .frame(height: availableHeight > 0 ? max(100, availableHeight - 118) : 130)
                indicatorDate(inspected?.time)
            }
        }
    }
}

private func indicatorDomain(_ points: [NativeIndicatorPoint]) -> ClosedRange<Double> {
    (Double(points.first?.id ?? 0) - 0.6)...(Double(points.last?.id ?? 1) + 0.6)
}

private func indicatorTimeAxis(_ points: [NativeIndicatorPoint]) -> some AxisContent {
    let ticks = StockChartLabels.ticks(times: points.map(\.time),
                                       positions: points.map { Double($0.id) }, maxCount: 4)
    return AxisMarks(position: .bottom, values: ticks.map(\.position)) { value in
        AxisValueLabel {
            if let position = value.as(Double.self), let tick = ticks.first(where: { $0.position == position }) {
                Text(tick.label).font(.caption2).monospacedDigit().multilineTextAlignment(.center)
            }
        }
    }
}

private func indicatorAxis(_ scale: NativeIndicatorScale) -> some AxisContent {
    AxisMarks(position: .trailing, values: scale.ticks) { value in
        AxisGridLine().foregroundStyle(.secondary.opacity(0.1))
        AxisValueLabel {
            if let value = value.as(Double.self) {
                Text(scale.label(value))
                    .font(.caption2).monospacedDigit().frame(width: 52, alignment: .trailing)
            }
        }
    }
}

private var indicatorEmpty: some View {
    Text("暂无指标数据").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 140)
}

private func indicatorValue(_ title: String, _ value: Double?, color: Color, scale: NativeIndicatorScale) -> some View {
    Text("\(title) \(scale.valueLabel(value))")
        .font(.caption).monospacedDigit().foregroundStyle(color).fixedSize(horizontal: false, vertical: true)
}

private func indicatorDate(_ time: String?) -> some View {
    Text(StockChartLabels.detail(time)).font(.caption2).monospacedDigit().foregroundStyle(.secondary)
}

private struct FundSizeFlow: Identifiable {
    let name: String
    let buy: Double?
    let sell: Double?
    let color: Color
    var id: String { name }
    var net: Double? {
        guard let buy, let sell else { return nil }
        return buy - sell
    }
}

private struct FundSlice: Identifiable {
    let name: String
    let amount: Double?
    let color: Color
    var id: String { name }
}

struct FundCard: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    @Environment(\.workspaceCardWidth) private var availableWidth
    let panel: FundPanel
    @State private var selectedDate: String?
    private var history: [FundHistory] {
        Array(panel.history.sorted { $0.tradeDate < $1.tradeDate }.suffix(5))
    }
    private var historyTicks: [StockChartTick] {
        StockChartLabels.ticks(times: history.map(\.tradeDate), maxCount: 4)
    }
    private var historyAxisDates: [String] {
        historyTicks.compactMap { tick in
            let index = Int(tick.position)
            return history.indices.contains(index) ? history[index].tradeDate : nil
        }
    }
    private func historyAxisLabel(_ date: String) -> String {
        guard let index = history.firstIndex(where: { $0.tradeDate == date }),
              let tick = historyTicks.first(where: { $0.position == Double(index) }) else { return StockChartLabels.day(date) }
        return tick.label
    }
    private var selected: FundHistory? {
        history.first { $0.tradeDate == selectedDate } ?? history.last
    }
    private var inspectedDate: String? { selected?.tradeDate ?? panel.asOf }
    private var dateSelection: Binding<String?> {
        Binding(get: { inspectedDate }, set: { date in
            guard let date, history.contains(where: { $0.tradeDate == date }) else { return }
            selectedDate = date
        })
    }
    private var selectedFlows: [FundSizeFlow] {
        if let selected { return flows(selected) }
        let m = panel.metrics
        return flowValues(m.buyExtraLargeAmount, m.sellExtraLargeAmount, m.buyLargeAmount, m.sellLargeAmount,
                          m.buyMediumAmount, m.sellMediumAmount, m.buySmallAmount, m.sellSmallAmount)
    }
    private var selectedMainInflow: Double? {
        let main = selectedFlows.prefix(2).compactMap(\.net)
        guard main.count == 2, main.allSatisfy(\.isFinite) else { return nil }
        return main.reduce(0, +)
    }
    private var slices: [FundSlice] {
        let buyColors = [0xdc2626, 0xf59e0b, 0xfbbf24, 0xfde68a].map(Self.palette)
        let sellColors = [0x059669, 0x10b981, 0x6ee7b7, 0xa7f3d0].map(Self.palette)
        let buys = selectedFlows.enumerated().map { index, flow in
            FundSlice(name: "\(flow.name)买入", amount: flow.buy, color: buyColors[index])
        }
        let sells = selectedFlows.enumerated().map { index, flow in
            FundSlice(name: "\(flow.name)卖出", amount: flow.sell, color: sellColors[index])
        }
        return buys + sells
    }
    private var total: Double? {
        let amounts = slices.compactMap(\.amount)
        guard amounts.count == 8, amounts.allSatisfy(\.isFinite) else { return nil }
        return amounts.reduce(0) { $0 + abs($1) }
    }
    private var hasHistoryFlows: Bool { history.contains { flows($0).contains { $0.net != nil } } }

    private var usesColumns: Bool {
        availableWidth >= 660
    }
    private var contentWidth: CGFloat { max(0, availableWidth - 32) }
    private var detailsWidth: CGFloat {
        usesColumns ? min(400, max(300, contentWidth * 0.49)) : contentWidth
    }
    private var historyHeight: CGFloat {
        guard availableHeight > 0 else { return 150 }
        if usesColumns { return max(110, availableHeight - 138) }
        let detailsAllowance: CGFloat = detailsWidth >= 300 ? 510 : 620
        return max(110, availableHeight - detailsAllowance)
    }
    private var mainLayout: AnyLayout {
        usesColumns ? AnyLayout(HStackLayout(alignment: .top, spacing: 16))
                    : AnyLayout(VStackLayout(alignment: .leading, spacing: 12))
    }
    private var distributionLayout: AnyLayout {
        detailsWidth >= 300 || availableWidth <= 0
            ? AnyLayout(HStackLayout(alignment: .center, spacing: 10))
            : AnyLayout(VStackLayout(alignment: .center, spacing: 8))
    }
    private var donutSize: CGFloat { 100 }

    var body: some View {
        card(title: "资金动向", subtitle: "最近 \(history.count) 个交易日") {
            mainLayout {
                historySection.frame(maxWidth: .infinity, alignment: .topLeading)
                detailsSection
                    .frame(width: usesColumns ? detailsWidth : nil)
                    .frame(maxWidth: usesColumns ? nil : .infinity, alignment: .topLeading)
            }
        }
        .onChange(of: history.map(\.tradeDate)) { _, dates in
            if let selectedDate, !dates.contains(selectedDate) { self.selectedDate = nil }
        }
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 12) { tierLegend }
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 6) {
                    tierLegend
                }
            }
            if hasHistoryFlows {
                Chart {
                    RuleMark(y: .value("零轴", 0.0)).foregroundStyle(.secondary.opacity(0.3))
                    ForEach(history) { point in
                        ForEach(flows(point)) { flow in
                            if let value = flow.net {
                                BarMark(x: .value("日期", point.tradeDate), y: .value("净流入", value), stacking: .standard)
                                    .foregroundStyle(flow.color)
                            }
                        }
                    }
                }
                .chartXScale(domain: history.map(\.tradeDate))
                .chartXSelection(value: dateSelection)
                .chartXAxis {
                    AxisMarks(values: historyAxisDates) { value in
                        AxisValueLabel {
                            if let date = value.as(String.self) {
                                Text(historyAxisLabel(date)).font(.caption2).monospacedDigit()
                            }
                        }
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisGridLine().foregroundStyle(.secondary.opacity(0.1))
                        AxisValueLabel {
                            if let value = value.as(Double.self) { Text(money(value)).font(.caption2).monospacedDigit() }
                        }
                    }
                }
                .chartBackground { proxy in
                    GeometryReader { geometry in
                        if let anchor = proxy.plotFrame, let date = inspectedDate,
                           let x = proxy.position(forX: date) {
                            let plot = geometry[anchor]
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.secondary.opacity(0.10))
                                .frame(width: max(1, plot.width / CGFloat(max(history.count, 1))), height: plot.height)
                                .position(x: plot.minX + x, y: plot.midY)
                        }
                    }.allowsHitTesting(false)
                }
                .frame(height: historyHeight)
                Text("拖动柱状图选择交易日").font(.caption2).foregroundStyle(.secondary)
            } else {
                Text("暂无分档资金历史").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 80, alignment: .center)
            }
        }
    }

    @ViewBuilder private var tierLegend: some View {
        ForEach(selectedFlows) { flow in
            Label("\(flow.name)净", systemImage: "circle.fill")
                .font(.caption2).foregroundStyle(flow.color).lineLimit(1)
        }
    }

    private var detailsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 5) {
                Text("\(StockChartLabels.day(inspectedDate)) · 所选交易日")
                    .font(.caption2).foregroundStyle(.secondary)
                HStack {
                    Text("主力净流入").font(.caption.weight(.medium))
                    Spacer(minLength: 4)
                    Text(money(selectedMainInflow, signed: true))
                        .font(.subheadline.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(AppStyle.movement(selectedMainInflow))
                }
            }
            distributionLayout {
                distributionChart.frame(width: donutSize, height: donutSize)
                distributionLegend.frame(maxWidth: .infinity)
            }
            .frame(maxWidth: .infinity)
            VStack(spacing: 6) {
                ForEach(selectedFlows) { flow in fundValueLine("\(flow.name)净流入", flow.net) }
            }
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text("最新汇总 · \(StockChartLabels.day(panel.asOf))")
                    .font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                fundValueLine("当日主力", panel.metrics.latestMainInflow)
                fundValueLine("五日主力合计", panel.metrics.mainInflow5d)
                valueLine("当日主力占比", panel.metrics.latestMainRatio, suffix: "%")
            }
        }
    }

    @ViewBuilder private var distributionChart: some View {
        if let total, total > 0 {
            Chart {
                ForEach(slices) { slice in
                    if let amount = slice.amount, amount != 0 {
                        SectorMark(angle: .value("金额", abs(amount)), innerRadius: .ratio(0.45), angularInset: 1)
                            .foregroundStyle(slice.color)
                    }
                }
            }.chartLegend(.hidden)
        } else {
            ZStack {
                Circle().stroke(Color.secondary.opacity(0.12), lineWidth: donutSize * 0.20)
                    .padding(donutSize * 0.1)
                Text(total == nil ? "数据不完整" : "暂无金额")
                    .font(.caption2).foregroundStyle(.secondary).multilineTextAlignment(.center)
            }
        }
    }

    private var distributionLegend: some View {
        VStack(spacing: 4) {
            ForEach(slices) { slice in
                HStack(spacing: 5) {
                    Circle().fill(slice.color).frame(width: 5, height: 5)
                    Text(slice.name).lineLimit(1)
                    Spacer(minLength: 3)
                    Text(money(slice.amount)).lineLimit(1)
                    Text(share(slice.amount))
                        .lineLimit(1).frame(width: 38, alignment: .trailing)
                }
                .font(.caption2).monospacedDigit().foregroundStyle(.secondary)
            }
        }
    }

    private func share(_ amount: Double?) -> String {
        guard let total, let amount, amount.isFinite else { return "—" }
        return String(format: "%.1f%%", total > 0 ? abs(amount) / total * 100 : 0)
    }
    private func money(_ amount: Double?, signed: Bool = false) -> String {
        guard let amount, amount.isFinite else { return "—" }
        // Server metrics are already yuan, including the upstream Tushare conversion.
        return String(format: signed ? "%+.2f亿" : "%.2f亿", amount / 100_000_000)
    }
    private func fundValueLine(_ title: String, _ amount: Double?) -> some View {
        HStack {
            Text(title).foregroundStyle(.secondary)
            Spacer(minLength: 4)
            Text(money(amount, signed: true)).monospacedDigit().foregroundStyle(AppStyle.movement(amount))
        }.font(.caption)
    }
    private static func palette(_ hex: Int) -> Color {
        Color(red: Double((hex >> 16) & 0xff) / 255,
              green: Double((hex >> 8) & 0xff) / 255,
              blue: Double(hex & 0xff) / 255)
    }
    private func flows(_ point: FundHistory) -> [FundSizeFlow] {
        flowValues(point.buyExtraLargeAmount, point.sellExtraLargeAmount, point.buyLargeAmount, point.sellLargeAmount,
                   point.buyMediumAmount, point.sellMediumAmount, point.buySmallAmount, point.sellSmallAmount)
    }
    private func flowValues(_ extraBuy: Double?, _ extraSell: Double?, _ largeBuy: Double?, _ largeSell: Double?,
                            _ mediumBuy: Double?, _ mediumSell: Double?, _ smallBuy: Double?, _ smallSell: Double?) -> [FundSizeFlow] {
        [FundSizeFlow(name: "超大单", buy: extraBuy, sell: extraSell, color: Self.palette(0xdc2626)),
         FundSizeFlow(name: "大单", buy: largeBuy, sell: largeSell, color: Self.palette(0xf59e0b)),
         FundSizeFlow(name: "中单", buy: mediumBuy, sell: mediumSell, color: Self.palette(0x10b981)),
         FundSizeFlow(name: "小单", buy: smallBuy, sell: smallSell, color: Self.palette(0x3b82f6))]
    }
}

struct ChipCard: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    let panel: ChipPanel
    private var levels: [ChipCostLevel] {
        [ChipCostLevel(title: "5% 深获利", value: panel.cost5, color: AppStyle.up),
         ChipCostLevel(title: "15% 浅获利", value: panel.cost15, color: AppStyle.up.opacity(0.65)),
         ChipCostLevel(title: "50% 主力区", value: panel.cost50, color: .blue),
         ChipCostLevel(title: "85% 浅套牢", value: panel.cost85, color: AppStyle.down.opacity(0.65)),
         ChipCostLevel(title: "95% 套牢线", value: panel.cost95, color: AppStyle.down)]
    }
    var body: some View {
        card(title: "筹码成本", subtitle: StockChartLabels.day(panel.asOf)) {
            Chart(levels) { level in
                if let value = level.value {
                    BarMark(x: .value("成本", value), y: .value("成本分位", level.title), height: .fixed(12))
                        .foregroundStyle(level.color)
                        .annotation(position: .trailing) {
                            Text(AppStyle.price(value)).font(.caption2).monospacedDigit()
                        }
                }
            }
            .chartXAxis(.hidden).frame(height: availableHeight > 0 ? max(140, availableHeight - 116) : 150)
            valueLine("平均成本", panel.average, color: AppStyle.ink)
            valueLine("获利比例", panel.winnerRate.map { $0 * 100 }, suffix: "%", color: AppStyle.up)
        }
    }
}

private struct ChipCostLevel: Identifiable {
    let title: String
    let value: Double?
    let color: Color
    var id: String { title }
}

struct ChipDistributionCard: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    let data: ChipDistributionResponse?
    let currentPrice: Double?
    var body: some View {
        card(title: "筹码峰", subtitle: StockChartLabels.day(data?.end)) {
            ChipDistributionPlot(data: data, currentPrice: currentPrice)
                .frame(height: availableHeight > 0 ? max(150, availableHeight - 155) : 240)
            if let data, !data.rows.isEmpty {
                ChipDistributionSummary(data: data, currentPrice: currentPrice)
            }
        }
    }
}

struct ChipDistributionPlot: View {
    let data: ChipDistributionResponse?
    let currentPrice: Double?
    var priceDomain: ClosedRange<Double>? = nil
    var compact = false
    private var rows: [ChipDistributionRow] {
        (data?.rows ?? []).filter { $0.price.isFinite && $0.percent.isFinite && $0.percent > 0 }.sorted { $0.price < $1.price }
    }
    private var domain: ClosedRange<Double> {
        if let priceDomain { return priceDomain }
        let low = rows.first?.price ?? 0
        let high = rows.last?.price ?? 1
        let padding = max((high - low) * 0.05, abs(high) * 0.001, 0.01)
        return (low - padding)...(high + padding)
    }
    private var barHalfStep: Double {
        let diffs = zip(rows.dropFirst(), rows).map { $0.price - $1.price }.filter { $0 > 0 }
        return (diffs.min() ?? max((domain.upperBound - domain.lowerBound) / 60, 0.01)) * 0.45
    }
    private var barGradient: LinearGradient {
        guard let profit = chipProfit(data, price: currentPrice) else {
            return LinearGradient(colors: [.secondary], startPoint: .leading, endPoint: .trailing)
        }
        let ratio = min(max(profit, 0), 1)
        if ratio == 0 { return LinearGradient(colors: [AppStyle.down], startPoint: .leading, endPoint: .trailing) }
        if ratio == 1 { return LinearGradient(colors: [AppStyle.up], startPoint: .leading, endPoint: .trailing) }
        return LinearGradient(stops: [.init(color: AppStyle.up, location: 0),
                                      .init(color: AppStyle.up, location: max(0, ratio - 0.04)),
                                      .init(color: AppStyle.down, location: min(1, ratio + 0.04)),
                                      .init(color: AppStyle.down, location: 1)], startPoint: .leading, endPoint: .trailing)
    }
    var body: some View {
        let renderedRows = rows
        let halfStep = barHalfStep
        let gradient = barGradient
        let resolvedDomain = domain
        return Group {
            if renderedRows.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "chart.bar.xaxis").foregroundStyle(.tertiary)
                    Text(data?.warning.map(chipWarningText) ?? "暂无筹码分布数据").font(.caption).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Chart {
                    ForEach(Array(renderedRows.enumerated()), id: \.offset) { pair in
                        RectangleMark(xStart: .value("起点", 0), xEnd: .value("占比", pair.element.percent),
                                      yStart: .value("价格下界", pair.element.price - halfStep),
                                      yEnd: .value("价格上界", pair.element.price + halfStep))
                            .foregroundStyle(gradient)
                            .alignsMarkStylesWithPlotArea(false)
                    }
                    if let price = currentPrice ?? data?.currentPrice {
                        RuleMark(y: .value("现价", price)).foregroundStyle(Color.yellow.opacity(0.9))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))
                    }
                    if let average = data?.averageCost {
                        RuleMark(y: .value("平均成本", average)).foregroundStyle(.orange)
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [5, 3]))
                    }
                }
                .chartYScale(domain: resolvedDomain).chartXScale(domain: 0...max(renderedRows.map(\.percent).max() ?? 1, 0.001))
                .chartPlotStyle { $0.clipped() }
                .chartXAxis(.hidden)
                .chartYAxis {
                    if !compact {
                        AxisMarks(position: .trailing, values: .automatic(desiredCount: 5)) { value in
                            AxisGridLine().foregroundStyle(.secondary.opacity(0.1))
                            AxisValueLabel {
                                if let price = value.as(Double.self) { Text(AppStyle.price(price)).font(.caption2) }
                            }
                        }
                    }
                }
            }
        }
    }
}

struct ChipDistributionSummary: View {
    let data: ChipDistributionResponse
    let currentPrice: Double?
    var body: some View {
        VStack(spacing: 8) {
            HStack {
                Text("获利 \(AppStyle.percent(chipProfit(data, price: currentPrice).map { $0 * 100 }))").foregroundStyle(AppStyle.up)
                Spacer()
                Text("套牢 \(AppStyle.percent(chipProfit(data, price: currentPrice).map { (1 - $0) * 100 }))").foregroundStyle(AppStyle.down)
            }
            HStack {
                Text("现价 \(AppStyle.price(currentPrice ?? data.currentPrice))")
                Spacer()
                Text("平均成本 \(AppStyle.price(data.averageCost))").foregroundStyle(.orange)
            }
            HStack {
                Text("90% 区间 \(AppStyle.price(data.cost5))–\(AppStyle.price(data.cost95))")
                Spacer()
                Text("集中度 \(AppStyle.percent(data.concentration.map { $0 * 100 }))")
            }.foregroundStyle(.secondary)
            if let warning = data.warning { Text(chipWarningText(warning)).foregroundStyle(.secondary) }
        }.font(.caption2).monospacedDigit()
    }
}

private func chipWarningText(_ warning: String) -> String {
    if warning.contains("live_quote") { return "实时行情暂缺，显示已保存数据" }
    if warning.contains("insufficient") { return "区间内历史数据不足" }
    return "部分筹码数据暂缺"
}

private func chipProfit(_ data: ChipDistributionResponse?, price: Double?) -> Double? {
    guard let data else { return nil }
    if let price = price ?? data.currentPrice {
        let total = data.rows.reduce(0) { $0 + max(0, $1.percent) }
        if total > 0 { return data.rows.filter { $0.price <= price }.reduce(0) { $0 + max(0, $1.percent) } / total }
    }
    return data.winnerRate
}

struct PeersCard: View {
    let peers: [PeerStock]
    @ObservedObject var model: AppModel
    var body: some View {
        card(title: "同业对比", subtitle: "按当日涨幅") {
            ForEach(peers.prefix(7)) { peer in
                Button { model.selectedCode = peer.code } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(peer.name).foregroundStyle(AppStyle.ink)
                            Text(peer.code).font(.caption2).foregroundStyle(.secondary).monospaced()
                        }
                        Spacer()
                        Text(AppStyle.price(peer.price)).foregroundStyle(AppStyle.ink).monospacedDigit()
                        Text(AppStyle.change(peer.changePct)).foregroundStyle(AppStyle.movement(peer.changePct))
                            .monospacedDigit().frame(width: 68, alignment: .trailing)
                    }
                }
                .buttonStyle(.plain)
                if peer.id != peers.prefix(7).last?.id { Divider() }
            }
        }
    }
}

struct ConceptsCard: View {
    let concepts: [String]
    var body: some View {
        card(title: "行业与概念", subtitle: "所属板块") {
            FlowTags(items: concepts)
        }
    }
}

private struct AnnouncementsCard: View {
    let items: [Announcement]
    var body: some View {
        card(title: "公司公告", subtitle: "最近披露") {
            ForEach(items.prefix(7)) { item in
                if let raw = item.url, let url = URL(string: raw) {
                    Link(destination: url) { announcementRow(item) }
                        .buttonStyle(.plain)
                } else {
                    announcementRow(item)
                }
                if item.id != items.prefix(7).last?.id { Divider() }
            }
        }
    }

    private func announcementRow(_ item: Announcement) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(item.title ?? "公告").font(.subheadline).foregroundStyle(AppStyle.ink)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Text(StockChartLabels.day(item.date))
                if let category = item.category { Text("· \(category)") }
                Spacer()
                Image(systemName: "arrow.up.right").font(.caption2)
            }
            .font(.caption2).foregroundStyle(.secondary)
        }
    }
}

private struct FlowTags: View {
    let items: [String]
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 120), spacing: 8, alignment: .leading)],
                  alignment: .leading, spacing: 8) {
                ForEach(items, id: \.self) { item in
                    Text(item).font(.caption).fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background(AppStyle.canvas, in: Capsule())
                }
        }
    }
}

private func card<Content: View>(title: String, subtitle: String,
                                 @ViewBuilder content: () -> Content) -> some View {
    VStack(alignment: .leading, spacing: 10) {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.headline)
                Spacer()
                Text(subtitle).font(.caption2).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.headline)
                Text(subtitle).font(.caption2).foregroundStyle(.secondary)
            }
        }
        content()
    }
    .padding(16).frame(maxWidth: .infinity, alignment: .leading)
    .background(.white, in: RoundedRectangle(cornerRadius: 20))
}

private func metricLine(_ leftTitle: String, _ left: Double?, _ rightTitle: String, _ right: Double?) -> some View {
    HStack {
        Text(leftTitle).foregroundStyle(.secondary)
        Text(left.map { String(format: "%.2f", $0) } ?? "—").monospacedDigit()
        Spacer()
        Text(rightTitle).foregroundStyle(.secondary)
        Text(right.map { String(format: "%.2f", $0) } ?? "—").monospacedDigit()
    }
    .font(.caption)
}

private func valueLine(_ title: String, _ value: Double?, compact: Bool = false,
                       suffix: String = "", color: Color? = nil) -> some View {
    HStack {
        Text(title).foregroundStyle(.secondary)
        Spacer()
        Text(compact ? AppStyle.compact(value) : (value.map { String(format: "%.2f", $0) } ?? "—") + suffix)
            .monospacedDigit().foregroundStyle(color ?? AppStyle.movement(value))
    }
    .font(.caption)
}
