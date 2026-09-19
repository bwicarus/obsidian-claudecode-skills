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
    let stock: Stock
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("五档盘口").font(.headline)
            if (stock.asks ?? []).isEmpty && (stock.bids ?? []).isEmpty {
                Text("暂无五档数据").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 90)
            } else if compact {
                HStack(alignment: .top, spacing: 22) {
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
        .padding(16)
        .background(.white, in: RoundedRectangle(cornerRadius: 18))
    }

    private func bookSection(_ title: String, rows: [OrderLevel], tint: Color, reversed: Bool = false) -> some View {
        VStack(spacing: 8) {
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
                .font(.caption.monospacedDigit()).lineLimit(1).minimumScaleFactor(0.8)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func smallValue(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit()).foregroundStyle(AppStyle.ink)
        }
    }
}

struct StockValuationCard: View {
    let detail: StockResponse

    var body: some View {
        card(title: "估值与表现", subtitle: detail.asOf ?? "最新资料") {
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

enum WorkspaceChartMode: Hashable { case adaptive, kline, intraday, withChips }

struct MarketChartSection: View {
    @ObservedObject var model: AppModel
    let stockCode: String
    var mode: WorkspaceChartMode = .adaptive
    @State private var lastCandlePeriod: ChartPeriod = .day

    private var isIntraday: Bool { mode == .intraday || (mode == .adaptive && model.chartPeriod == .intraday) }
    private var candlePeriod: ChartPeriod { model.klinePeriod }
    private var candles: [Candle] { model.displayedKlineCandles }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                if mode == .adaptive {
                Picker("图表类型", selection: Binding(get: { model.chartPeriod == .intraday }, set: {
                    model.chartPeriod = $0 ? .intraday : lastCandlePeriod
                })) {
                    Text("分时走势").tag(true)
                    Text("K 线").tag(false)
                }
                .pickerStyle(.segmented).frame(maxWidth: 260)
                } else {
                    Text(isIntraday ? "分时走势" : (mode == .withChips ? "K 线与筹码峰" : "K 线走势")).font(.headline)
                }
                Spacer(minLength: 0)
                if !isIntraday {
                    Menu {
                        ForEach(ChartPeriod.allCases.filter { $0 != .intraday }) { period in
                            Button {
                                lastCandlePeriod = period
                                if mode == .adaptive { model.chartPeriod = period }
                                else { model.klinePeriod = period }
                            } label: {
                                if period == candlePeriod {
                                    Label(candleIntervalTitle(period), systemImage: "checkmark")
                                } else { Text(candleIntervalTitle(period)) }
                            }
                        }
                    } label: {
                        HStack(spacing: 5) {
                            Text(candleIntervalTitle(candlePeriod))
                            Image(systemName: "chevron.down").font(.caption2)
                        }
                        .font(.caption.weight(.medium)).padding(.vertical, 12)
                    }
                    .accessibilityLabel("K 线精度，\(candleIntervalTitle(candlePeriod))")
                }
            }
            if let error = model.chartError {
                Label(error, systemImage: "wifi.exclamationmark")
                    .font(.caption).foregroundStyle(.red)
            }
            if isIntraday {
                if let intraday = model.displayedIntraday, !intraday.rows.isEmpty {
                    IntradayChart(data: intraday, stockCode: stockCode, annotations: model.annotations,
                                  onContextChange: { snapshot in
                                      await model.updateChartContext(snapshot, sourceID: "\(mode):intraday")
                                  })
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
                            onRangeChange: { count in
                                await model.publishVoiceContext(action: "调整图表可见区间：\(count) 根", kind: "chart_range")
                            }, chipDistribution: model.chipDistribution, showsChips: mode == .withChips,
                            externalContext: model.chartSnapshotForKline, currentPrice: model.displayedStock?.price)
                    .id("\(stockCode):\(candlePeriod.rawValue):\(mode)")
            } else if model.isLoadingChart {
                chartLoading
            } else {
                emptyChart("暂无这个周期的 K 线")
            }
        }
        .onChange(of: model.chartPeriod, initial: true) { _, period in
            if period != .intraday { lastCandlePeriod = period }
        }
    }

    private func candleIntervalTitle(_ period: ChartPeriod) -> String {
        switch period {
        case .m5: return "每根 5 分钟"
        case .m15: return "每根 15 分钟"
        case .m30: return "每根 30 分钟"
        case .m60: return "每根 60 分钟"
        case .week: return "每根 1 周"
        case .month: return "每根 1 月"
        default: return "每根 1 天"
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
    @Environment(\.workspaceCardHeight) private var availableHeight
    let data: IntradayResponse
    let stockCode: String
    @ObservedObject var annotations: AnnotationStore
    let onContextChange: (VoiceChartSnapshot) async -> Void
    @State private var selectedTime: String?
    @State private var window = 0..<242

    private var points: [SessionIntradayPoint] {
        data.rows.compactMap(SessionIntradayPoint.init).sorted { $0.minute < $1.minute }
    }
    private var visiblePoints: [SessionIntradayPoint] {
        points.filter { window.contains(Int($0.minute)) }
    }
    private var xDomain: ClosedRange<Double> {
        Double(window.lowerBound)...Double(max(window.lowerBound + 1, window.upperBound - 1))
    }
    private var overviewValues: [Double?] {
        var values = [Double?](repeating: nil, count: 242)
        for item in points { values[Int(item.minute)] = item.point.price }
        return values
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
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("当日分时").font(.headline)
                    Text(data.tradeDate.isEmpty ? "常规交易时段" : "\(data.tradeDate) · 常规交易时段")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let latest = inspected?.point {
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(AppStyle.price(latest.price)).font(.title3.weight(.semibold)).monospacedDigit()
                        Text(latest.time).font(.caption).foregroundStyle(.secondary).monospacedDigit()
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
            .frame(height: availableHeight > 0 ? max(150, availableHeight - 300) : 300)
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
            .frame(height: 70)
            VStack(spacing: 6) {
                HStack {
                    Text("\(sessionTime(window.lowerBound)) — \(sessionTime(window.upperBound - 1))")
                    Spacer()
                    Button("全天") {
                        window = 0..<242
                        selectedTime = nil
                        Task { await onContextChange(voiceContextSnapshot) }
                    }
                    .disabled(window == 0..<242)
                }
                .font(.caption).foregroundStyle(.secondary)
                ChartRangeNavigator(values: overviewValues, selection: $window, minimumCount: 12,
                                    onEditingChanged: { editing in
                    if !editing { Task { await onContextChange(voiceContextSnapshot) } }
                })
                Text("两端缩放 · 中间平移")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(22).background(.white, in: RoundedRectangle(cornerRadius: 22))
        .task(id: voiceContextSnapshot) { await onContextChange(voiceContextSnapshot) }
        .onChange(of: data.tradeDate) { _, _ in selectedTime = nil; window = 0..<242 }
        .onChange(of: window) { _, _ in selectedTime = nil }
        .onChange(of: visiblePoints.map(\.id)) { _, times in
            if let selectedTime, !times.contains(selectedTime) { self.selectedTime = nil }
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

    var body: some View {
        card(title: "MACD", subtitle: "\(period.title) · 12 / 26 / 9") {
            if points.isEmpty { indicatorEmpty }
            else {
                HStack(spacing: 12) {
                    indicatorValue("DIF", inspected?.dif, color: AppStyle.accent)
                    indicatorValue("DEA", inspected?.dea, color: .orange)
                    indicatorValue("MACD", inspected?.histogram, color: AppStyle.movement(inspected?.histogram))
                }
                Chart {
                    RuleMark(y: .value("零轴", 0)).foregroundStyle(.secondary.opacity(0.3))
                    ForEach(points) { point in
                        if let histogram = point.histogram {
                            BarMark(x: .value("时间", Double(point.id)), y: .value("MACD", histogram), width: .ratio(0.6))
                                .foregroundStyle(AppStyle.movement(histogram).opacity(0.55))
                        }
                        if let dif = point.dif {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("DIF", dif), series: .value("指标", "DIF"))
                                .foregroundStyle(AppStyle.accent).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if let dea = point.dea {
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
                .chartXAxis(.hidden).chartYAxis { indicatorAxis }.chartLegend(.hidden)
                .frame(height: availableHeight > 0 ? max(90, availableHeight - 125) : 130)
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
    private var yDomain: ClosedRange<Double> {
        let values = points.flatMap { [$0.k, $0.d, $0.j].compactMap { $0 } }
        return min(0, (values.min() ?? 0) - 5)...max(100, (values.max() ?? 100) + 5)
    }

    var body: some View {
        card(title: "KDJ", subtitle: "\(period.title) · 9 / 3 / 3") {
            if points.isEmpty { indicatorEmpty }
            else {
                HStack(spacing: 18) {
                    indicatorValue("K", inspected?.k, color: AppStyle.accent)
                    indicatorValue("D", inspected?.d, color: .orange)
                    indicatorValue("J", inspected?.j, color: .purple)
                }
                Chart {
                    ForEach([20.0, 80.0], id: \.self) { value in
                        RuleMark(y: .value("参考", value)).foregroundStyle(.secondary.opacity(0.3))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    }
                    ForEach(points) { point in
                        if let k = point.k {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("K", k), series: .value("指标", "K"))
                                .foregroundStyle(AppStyle.accent).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if let d = point.d {
                            LineMark(x: .value("时间", Double(point.id)), y: .value("D", d), series: .value("指标", "D"))
                                .foregroundStyle(.orange).lineStyle(StrokeStyle(lineWidth: 1.4))
                        }
                        if let j = point.j {
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
                .chartYScale(domain: yDomain).chartXAxis(.hidden).chartYAxis { indicatorAxis }.chartLegend(.hidden)
                .frame(height: availableHeight > 0 ? max(90, availableHeight - 125) : 130)
                indicatorDate(inspected?.time)
            }
        }
    }
}

private func indicatorDomain(_ points: [NativeIndicatorPoint]) -> ClosedRange<Double> {
    (Double(points.first?.id ?? 0) - 0.6)...(Double(points.last?.id ?? 1) + 0.6)
}

private var indicatorAxis: some AxisContent {
    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
        AxisGridLine().foregroundStyle(.secondary.opacity(0.1))
        AxisValueLabel {
            if let value = value.as(Double.self) {
                Text(value.formatted(.number.precision(.fractionLength(0...2))))
                    .font(.caption2).monospacedDigit().frame(width: 52, alignment: .trailing)
            }
        }
    }
}

private var indicatorEmpty: some View {
    Text("暂无指标数据").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 140)
}

private func indicatorValue(_ title: String, _ value: Double?, color: Color) -> some View {
    Text("\(title) \(value.map { String(format: "%.3f", $0) } ?? "—")")
        .font(.caption).monospacedDigit().foregroundStyle(color).lineLimit(1).minimumScaleFactor(0.7)
}

private func indicatorDate(_ time: String?) -> some View {
    Text(time ?? "最新数据").font(.caption2).monospacedDigit().foregroundStyle(.secondary)
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
    let amount: Double
    let color: Color
    var id: String { name }
}

struct FundCard: View {
    @Environment(\.workspaceCardHeight) private var availableHeight
    @Environment(\.workspaceCardWidth) private var availableWidth
    let panel: FundPanel
    @State private var selectedDate: String?
    private var selected: FundHistory? { panel.history.first { $0.tradeDate == selectedDate } }
    private var selectedFlows: [FundSizeFlow] {
        if let selected { return flows(selected) }
        let m = panel.metrics
        return flowValues(m.buyExtraLargeAmount, m.sellExtraLargeAmount, m.buyLargeAmount, m.sellLargeAmount,
                          m.buyMediumAmount, m.sellMediumAmount, m.buySmallAmount, m.sellSmallAmount)
    }
    private var slices: [FundSlice] {
        let buyColors: [Color] = [AppStyle.up, .orange, .yellow, .yellow.opacity(0.5)]
        let sellColors: [Color] = [AppStyle.down, .green, .mint, .mint.opacity(0.5)]
        let buys = selectedFlows.enumerated().compactMap { index, flow in
            flow.buy.map { FundSlice(name: "\(flow.name)买入", amount: max(0, $0), color: buyColors[index]) }
        }
        let sells = selectedFlows.enumerated().compactMap { index, flow in
            flow.sell.map { FundSlice(name: "\(flow.name)卖出", amount: max(0, $0), color: sellColors[index]) }
        }
        return (buys + sells).filter { $0.amount > 0 }
    }
    private var total: Double { slices.reduce(0) { $0 + $1.amount } }
    private var hasHistoryFlows: Bool { panel.history.contains { flows($0).contains { $0.net != nil } } }

    private var usesColumns: Bool {
        availableWidth >= 560 && (availableHeight <= 0 || availableWidth / availableHeight >= 1.35)
    }
    private var contentWidth: CGFloat { max(0, availableWidth - 40) }
    private var detailsWidth: CGFloat {
        usesColumns ? min(390, max(220, contentWidth * 0.43)) : contentWidth
    }
    private var historyHeight: CGFloat {
        guard availableHeight > 0 else { return 150 }
        if usesColumns { return max(150, availableHeight - 130) }
        let detailsAllowance: CGFloat = detailsWidth >= 330 ? 440 : 560
        return max(120, availableHeight - detailsAllowance)
    }
    private var mainLayout: AnyLayout {
        usesColumns ? AnyLayout(HStackLayout(alignment: .top, spacing: 24))
                    : AnyLayout(VStackLayout(alignment: .leading, spacing: 18))
    }
    private var distributionLayout: AnyLayout {
        detailsWidth >= 330 || availableWidth <= 0
            ? AnyLayout(HStackLayout(alignment: .center, spacing: 14))
            : AnyLayout(VStackLayout(alignment: .center, spacing: 12))
    }
    private var donutSize: CGFloat { detailsWidth >= 370 ? 124 : 108 }

    var body: some View {
        card(title: "资金动向", subtitle: selectedDate ?? panel.asOf ?? "最新资料") {
            mainLayout {
                historySection.frame(maxWidth: .infinity, alignment: .topLeading)
                detailsSection
                    .frame(width: usesColumns ? detailsWidth : nil)
                    .frame(maxWidth: usesColumns ? nil : .infinity, alignment: .topLeading)
            }
        }
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            if hasHistoryFlows {
                Chart {
                    RuleMark(y: .value("零轴", 0)).foregroundStyle(.secondary.opacity(0.3))
                    ForEach(panel.history) { point in
                        ForEach(flows(point)) { flow in
                            if let value = flow.net {
                                BarMark(x: .value("日期", point.tradeDate), y: .value("净流入", value), stacking: .standard)
                                    .foregroundStyle(flow.color)
                            }
                        }
                    }
                }
                .chartXAxis(.hidden).chartXSelection(value: $selectedDate)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisGridLine().foregroundStyle(.secondary.opacity(0.1))
                        AxisValueLabel {
                            if let value = value.as(Double.self) { Text(AppStyle.compact(value)).font(.caption2) }
                        }
                    }
                }
                .frame(height: historyHeight)
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) { tierLegend }
                    LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 6) {
                        tierLegend
                    }
                }
                Text("拖动柱状图选择交易日").font(.caption2).foregroundStyle(.secondary)
            } else {
                Text("暂无分档资金历史").font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 80, alignment: .center)
            }
        }
    }

    @ViewBuilder private var tierLegend: some View {
        ForEach(selectedFlows) { flow in
            Label(flow.name, systemImage: "circle.fill")
                .font(.caption2).foregroundStyle(flow.color).lineLimit(1)
        }
    }

    private var detailsSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            if !slices.isEmpty {
                distributionLayout {
                    Chart(slices) { slice in
                        SectorMark(angle: .value("金额", slice.amount), innerRadius: .ratio(0.68), angularInset: 1)
                            .foregroundStyle(slice.color)
                    }
                    .chartLegend(.hidden).frame(width: donutSize, height: donutSize)
                    distributionLegend.frame(maxWidth: .infinity)
                }
                .frame(maxWidth: .infinity)
            } else {
                Text("所选日期暂无分档买卖数据").font(.caption).foregroundStyle(.secondary)
            }
            VStack(spacing: 8) {
                ForEach(selectedFlows) { flow in valueLine("\(flow.name)净流入", flow.net, compact: true) }
                Divider().padding(.vertical, 2)
                valueLine("今日主力", panel.metrics.latestMainInflow, compact: true)
                valueLine("五日主力", panel.metrics.mainInflow5d, compact: true)
                valueLine("主力占比", panel.metrics.latestMainRatio, suffix: "%")
            }
        }
    }

    private var distributionLegend: some View {
        VStack(spacing: 6) {
            ForEach(slices) { slice in
                HStack(spacing: 5) {
                    Circle().fill(slice.color).frame(width: 5, height: 5)
                    Text(slice.name).lineLimit(1)
                    Spacer(minLength: 3)
                    Text(AppStyle.compact(slice.amount)).lineLimit(1)
                    Text(String(format: "%.1f%%", total > 0 ? slice.amount / total * 100 : 0))
                        .lineLimit(1).frame(width: 38, alignment: .trailing)
                }
                .font(.system(size: 10)).monospacedDigit().foregroundStyle(.secondary)
                .minimumScaleFactor(0.8)
            }
        }
    }

    private func flows(_ point: FundHistory) -> [FundSizeFlow] {
        flowValues(point.buyExtraLargeAmount, point.sellExtraLargeAmount, point.buyLargeAmount, point.sellLargeAmount,
                   point.buyMediumAmount, point.sellMediumAmount, point.buySmallAmount, point.sellSmallAmount)
    }
    private func flowValues(_ extraBuy: Double?, _ extraSell: Double?, _ largeBuy: Double?, _ largeSell: Double?,
                            _ mediumBuy: Double?, _ mediumSell: Double?, _ smallBuy: Double?, _ smallSell: Double?) -> [FundSizeFlow] {
        [FundSizeFlow(name: "超大单", buy: extraBuy, sell: extraSell, color: AppStyle.up),
         FundSizeFlow(name: "大单", buy: largeBuy, sell: largeSell, color: .orange),
         FundSizeFlow(name: "中单", buy: mediumBuy, sell: mediumSell, color: AppStyle.down),
         FundSizeFlow(name: "小单", buy: smallBuy, sell: smallSell, color: .blue)]
    }
}

struct ChipCard: View {
    let panel: ChipPanel
    private var levels: [ChipCostLevel] {
        [ChipCostLevel(title: "5% 深获利", value: panel.cost5, color: AppStyle.up),
         ChipCostLevel(title: "15% 浅获利", value: panel.cost15, color: AppStyle.up.opacity(0.65)),
         ChipCostLevel(title: "50% 主力区", value: panel.cost50, color: .blue),
         ChipCostLevel(title: "85% 浅套牢", value: panel.cost85, color: AppStyle.down.opacity(0.65)),
         ChipCostLevel(title: "95% 套牢线", value: panel.cost95, color: AppStyle.down)]
    }
    var body: some View {
        card(title: "筹码成本", subtitle: panel.asOf ?? "最新") {
            Chart(levels) { level in
                if let value = level.value {
                    BarMark(x: .value("成本", value), y: .value("成本分位", level.title), height: .fixed(12))
                        .foregroundStyle(level.color)
                        .annotation(position: .trailing) {
                            Text(AppStyle.price(value)).font(.caption2).monospacedDigit()
                        }
                }
            }
            .chartXAxis(.hidden).frame(height: 150)
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
        card(title: "筹码峰", subtitle: data?.end ?? "最新分布") {
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
            Text(item.title ?? "公告").font(.subheadline).foregroundStyle(AppStyle.ink).lineLimit(2)
            HStack {
                Text(item.date ?? "")
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
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(items, id: \.self) { item in
                    Text(item).font(.caption).padding(.horizontal, 10).padding(.vertical, 6)
                        .background(AppStyle.canvas, in: Capsule())
                }
            }
        }
    }
}

private func card<Content: View>(title: String, subtitle: String,
                                 @ViewBuilder content: () -> Content) -> some View {
    VStack(alignment: .leading, spacing: 14) {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.headline)
            Spacer()
            Text(subtitle).font(.caption2).foregroundStyle(.secondary)
        }
        content()
    }
    .padding(20).frame(maxWidth: .infinity, alignment: .leading)
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
