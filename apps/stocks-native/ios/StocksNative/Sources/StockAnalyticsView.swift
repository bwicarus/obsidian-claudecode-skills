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

private struct OrderBookPanel: View {
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

private struct StockValuationCard: View {
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

struct MarketChartSection: View {
    @ObservedObject var model: AppModel
    let stockCode: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(ChartPeriod.allCases) { period in
                        Button(period.title) { model.chartPeriod = period }
                            .buttonStyle(.bordered)
                            .buttonBorderShape(.capsule)
                            .tint(model.chartPeriod == period ? AppStyle.accent : .secondary)
                    }
                }
                .padding(.horizontal, 2)
            }
            if let error = model.chartError {
                Label(error, systemImage: "wifi.exclamationmark")
                    .font(.caption).foregroundStyle(.red)
            }
            if model.chartPeriod == .intraday {
                if let intraday = model.displayedIntraday, !intraday.rows.isEmpty {
                    IntradayChart(data: intraday, stockCode: stockCode, annotations: model.annotations,
                                  onContextChange: model.updateChartContext)
                        .id(stockCode)
                } else if model.isLoadingChart {
                    chartLoading
                } else {
                    emptyChart("当天暂无分时数据")
                }
            } else if !model.displayedCandles.isEmpty {
                CandleChart(candles: model.displayedCandles, stockCode: stockCode, period: model.chartPeriod,
                            annotations: model.annotations, onContextChange: model.updateChartContext,
                            onRangeChange: { count in
                                await model.publishVoiceContext(action: "图表区间：\(count) 根", kind: "chart_range")
                            })
                    .id("\(stockCode):\(model.chartPeriod.rawValue)")
            } else if model.isLoadingChart {
                chartLoading
            } else {
                emptyChart("暂无这个周期的 K 线")
            }
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

private struct IntradayChart: View {
    let data: IntradayResponse
    let stockCode: String
    @ObservedObject var annotations: AnnotationStore
    let onContextChange: (VoiceChartSnapshot) async -> Void
    @State private var selectedTime: String?

    private var points: [SessionIntradayPoint] {
        data.rows.compactMap(SessionIntradayPoint.init).sorted { $0.minute < $1.minute }
    }
    private var selectedPoint: SessionIntradayPoint? {
        guard let selectedTime else { return nil }
        return points.first { $0.point.time == selectedTime }
    }
    private var inspected: SessionIntradayPoint? { selectedPoint ?? points.last }
    private var selection: Binding<Double?> {
        Binding(get: { selectedPoint?.minute }, set: { value in
            guard let value, value.isFinite else { selectedTime = nil; return }
            selectedTime = points.min { abs($0.minute - value) < abs($1.minute - value) }?.point.time
        })
    }

    private var voiceContextSnapshot: VoiceChartSnapshot {
        let point = inspected.map { VoiceChartPoint(time: $0.point.time, price: $0.point.price, volume: $0.point.volume) }
        let chart = VoiceChartContext(stockCode: stockCode, period: ChartPeriod.intraday.rawValue,
                                      kind: "intraday", firstVisibleTime: points.first?.point.time,
                                      lastVisibleTime: points.last?.point.time,
                                      visiblePointCount: points.count, selectedPoint: point,
                                      selectionSource: selectedPoint == nil ? "latest" : "cursor")
        return VoiceChartSnapshot(chart: chart,
                                  annotations: annotations.voiceContext(stockCode: stockCode, editing: false, tool: .pen))
    }

    private var domain: ClosedRange<Double> {
        let values = points.flatMap { [$0.point.price, $0.point.averagePrice].compactMap { $0 } }
        let low = values.min() ?? 0
        let high = values.max() ?? 1
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
                    if let previous = data.previousClose {
                        RuleMark(y: .value("昨收", previous))
                            .foregroundStyle(.secondary.opacity(0.35))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    }
                    ForEach(points) { item in
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
                    if let selectedPoint {
                        RuleMark(x: .value("选中", selectedPoint.minute))
                            .foregroundStyle(AppStyle.ink.opacity(0.3))
                            .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                        PointMark(x: .value("选中", selectedPoint.minute), y: .value("价格", selectedPoint.point.price))
                            .foregroundStyle(AppStyle.accent).symbolSize(25)
                    }
                }
                .chartXScale(domain: 0.0...241.0, range: .plotDimension(padding: 0))
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
                    AxisMarks(values: [0.0, 120.5, 241.0]) { value in
                        AxisValueLabel {
                            if let minute = value.as(Double.self) {
                                Text(minute == 0 ? "09:30" : (minute == 241 ? "15:00" : "11:30 / 13:00"))
                                    .font(.caption2)
                            }
                        }
                    }
                }
                NativeAnnotationCanvas(store: annotations, stockCode: stockCode,
                                       tool: .pen, color: "accent", isEditing: false)
                    .allowsHitTesting(false)
                if points.isEmpty {
                    ContentUnavailableView("交易时段内暂无分时数据", systemImage: "chart.xyaxis.line")
                        .background(.white)
                }
            }
            .frame(height: 300)
            HStack(spacing: 18) {
                Label("价格", systemImage: "minus").foregroundStyle(AppStyle.accent)
                Label("均价", systemImage: "minus").foregroundStyle(.orange)
                Spacer()
                if let previous = data.previousClose {
                    Text("昨收 \(AppStyle.price(previous))").foregroundStyle(.secondary)
                }
            }
            .font(.caption)
            Chart(points) { item in
                BarMark(x: .value("交易分钟", item.minute), y: .value("成交量", item.point.volume), width: .ratio(0.8))
                    .foregroundStyle(AppStyle.accent.opacity(0.38))
            }
            .chartXScale(domain: 0.0...241.0, range: .plotDimension(padding: 0))
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
        }
        .padding(22).background(.white, in: RoundedRectangle(cornerRadius: 22))
        .task(id: voiceContextSnapshot) { await onContextChange(voiceContextSnapshot) }
        .onChange(of: data.tradeDate) { _, _ in selectedTime = nil }
        .onChange(of: points.map(\.id)) { _, times in
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

private struct TechnicalCard: View {
    let panel: TechnicalPanel
    var body: some View {
        card(title: "MACD", subtitle: "技术 · \(panel.asOf ?? "最新资料")") {
            Chart {
                ForEach(panel.history) { point in
                    if let histogram = point.macdHist {
                        BarMark(x: .value("日期", point.tradeDate), y: .value("MACD", histogram))
                            .foregroundStyle(AppStyle.movement(histogram).opacity(0.42))
                    }
                    if let dif = point.macdDif {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("DIF", dif))
                            .foregroundStyle(by: .value("指标", "DIF"))
                    }
                    if let dea = point.macdDea {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("DEA", dea))
                            .foregroundStyle(by: .value("指标", "DEA"))
                    }
                }
            }
            .chartForegroundStyleScale(["DIF": AppStyle.accent, "DEA": Color.orange])
            .chartXAxis(.hidden).frame(height: 110)
            metricLine("MA5", panel.metrics.ma5, "MA10", panel.metrics.ma10)
            metricLine("MA20", panel.metrics.ma20, "MA60", panel.metrics.ma60)
        }
    }
}

private struct KDJCard: View {
    let panel: TechnicalPanel

    var body: some View {
        card(title: "KDJ", subtitle: "技术 · \(panel.asOf ?? "最新资料")") {
            Chart {
                ForEach(panel.history) { point in
                    if let k = point.kdjK {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("数值", k))
                            .foregroundStyle(by: .value("指标", "K"))
                    }
                    if let d = point.kdjD {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("数值", d))
                            .foregroundStyle(by: .value("指标", "D"))
                    }
                    if let k = point.kdjK, let d = point.kdjD {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("数值", 3 * k - 2 * d))
                            .foregroundStyle(by: .value("指标", "J"))
                    }
                }
            }
            .chartForegroundStyleScale(["K": AppStyle.accent, "D": Color.orange, "J": Color.purple])
            .chartXAxis(.hidden).frame(height: 110)
            metricLine("K", panel.metrics.kdjK, "D", panel.metrics.kdjD)
            if let k = panel.metrics.kdjK, let d = panel.metrics.kdjD {
                valueLine("J", 3 * k - 2 * d, color: AppStyle.ink)
            }
        }
    }
}

private struct FundCard: View {
    let panel: FundPanel
    var body: some View {
        card(title: "资金动向", subtitle: panel.asOf ?? "主力净流入") {
            Chart(panel.history) { point in
                if let value = point.latestMainInflow {
                    BarMark(x: .value("日期", point.tradeDate), y: .value("净流入", value))
                        .foregroundStyle(AppStyle.movement(value).opacity(0.7))
                }
            }
            .chartXAxis(.hidden).frame(height: 110)
            valueLine("今日主力", panel.metrics.latestMainInflow, compact: true)
            valueLine("五日主力", panel.metrics.mainInflow5d, compact: true)
            valueLine("主力占比", panel.metrics.latestMainRatio, suffix: "%")
        }
    }
}

private struct ChipCard: View {
    let panel: ChipPanel
    private var levels: [(String, Double?)] {
        [("5%", panel.cost5), ("15%", panel.cost15), ("50%", panel.cost50),
         ("85%", panel.cost85), ("95%", panel.cost95)]
    }
    var body: some View {
        card(title: "筹码分布", subtitle: panel.asOf ?? "最新") {
            Chart(Array(levels.enumerated()), id: \.offset) { pair in
                let index = pair.offset
                let item = pair.element
                if let value = item.1 {
                    PointMark(x: .value("成本", value), y: .value("分位", index))
                        .foregroundStyle(AppStyle.accent).symbolSize(85)
                    RuleMark(x: .value("成本", value), yStart: .value("起", Double(index) - 0.28),
                             yEnd: .value("止", Double(index) + 0.28))
                        .foregroundStyle(AppStyle.accent.opacity(0.55))
                }
            }
            .chartYAxis {
                AxisMarks(values: Array(0..<levels.count)) { value in
                    AxisValueLabel { if let index = value.as(Int.self) { Text(levels[index].0) } }
                }
            }
            .frame(height: 150)
            valueLine("平均成本", panel.average, color: AppStyle.ink)
            valueLine("获利比例", panel.winnerRate.map { $0 * 100 }, suffix: "%", color: AppStyle.ink)
        }
    }
}

private struct PeersCard: View {
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

private struct ConceptsCard: View {
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
