import Charts
import SwiftUI

struct QuoteMetricGrid: View {
    let stock: Stock
    private let columns = [GridItem(.adaptive(minimum: 112), spacing: 10)]

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 10) {
            metric("今开", AppStyle.price(stock.open))
            metric("最高", AppStyle.price(stock.high), color: AppStyle.up)
            metric("最低", AppStyle.price(stock.low), color: AppStyle.down)
            metric("昨收", AppStyle.price(stock.prevClose))
            metric("成交量", AppStyle.compact(stock.volume))
            metric("成交额", AppStyle.compact(stock.turnover))
            metric("换手率", AppStyle.percent(stock.turnoverRate))
            metric("量比", stock.volumeRatio.map { String(format: "%.2f", $0) } ?? "—")
            metric("振幅", AppStyle.percent(stock.amplitude))
            metric("市盈率", stock.peDynamic.map { String(format: "%.2f", $0) } ?? "—")
            metric("市净率", stock.pb.map { String(format: "%.2f", $0) } ?? "—")
            metric("总市值", AppStyle.compact(stock.marketCap))
        }
    }

    private func metric(_ title: String, _ value: String, color: Color = AppStyle.ink) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.system(.subheadline, design: .rounded, weight: .medium))
                .monospacedDigit().foregroundStyle(color)
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(.white, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct OrderBookCard: View {
    let stock: Stock
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("五档盘口").font(.headline)
                Spacer()
                if let inner = stock.innerVolume, let outer = stock.outerVolume {
                    Text("内 \(AppStyle.compact(inner)) · 外 \(AppStyle.compact(outer))")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack(alignment: .top, spacing: 26) {
                levels(title: "卖盘", rows: Array((stock.asks ?? []).reversed()), tint: AppStyle.down)
                Divider()
                levels(title: "买盘", rows: stock.bids ?? [], tint: AppStyle.up)
            }
        }
        .padding(20).background(.white, in: RoundedRectangle(cornerRadius: 20))
    }

    private func levels(title: String, rows: [OrderLevel], tint: Color) -> some View {
        VStack(spacing: 7) {
            Text(title).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            ForEach(Array(rows.prefix(5).enumerated()), id: \.offset) { index, row in
                HStack {
                    Text("\(index + 1)").foregroundStyle(.tertiary)
                    Text(AppStyle.price(row.price)).foregroundStyle(tint)
                    Spacer()
                    Text(AppStyle.compact(row.volume)).foregroundStyle(.secondary)
                }
                .font(.caption.monospacedDigit())
            }
        }
        .frame(maxWidth: .infinity)
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
                if let intraday = model.intraday, !intraday.rows.isEmpty {
                    IntradayChart(data: intraday, stockCode: stockCode, annotations: model.annotations)
                } else if model.isLoadingChart {
                    chartLoading
                } else {
                    emptyChart("当天暂无分时数据")
                }
            } else if !model.displayedCandles.isEmpty {
                CandleChart(candles: model.displayedCandles, stockCode: stockCode, annotations: model.annotations)
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

private struct IntradayChart: View {
    let data: IntradayResponse
    let stockCode: String
    @ObservedObject var annotations: AnnotationStore

    private var domain: ClosedRange<Double> {
        let values = data.rows.flatMap { [$0.price, $0.averagePrice].compactMap { $0 } }
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
                    Text(data.tradeDate.isEmpty ? "实时行情" : data.tradeDate)
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let latest = data.rows.last {
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
                    ForEach(data.rows) { point in
                        LineMark(x: .value("时间", point.time), y: .value("价格", point.price))
                            .foregroundStyle(AppStyle.accent)
                            .lineStyle(StrokeStyle(lineWidth: 2))
                        AreaMark(x: .value("时间", point.time), yStart: .value("下沿", domain.lowerBound),
                                 yEnd: .value("价格", point.price))
                            .foregroundStyle(LinearGradient(colors: [AppStyle.accent.opacity(0.18), .clear],
                                                            startPoint: .top, endPoint: .bottom))
                        if let average = point.averagePrice {
                            LineMark(x: .value("时间", point.time), y: .value("均价", average))
                                .foregroundStyle(.orange.opacity(0.85))
                                .lineStyle(StrokeStyle(lineWidth: 1.2))
                        }
                    }
                }
                .chartYScale(domain: domain)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 5)) {
                        AxisGridLine().foregroundStyle(.gray.opacity(0.12))
                        AxisValueLabel().foregroundStyle(.secondary)
                    }
                }
                .chartXAxis { AxisMarks(values: ["09:30", "11:30", "15:00"]) }
                NativeAnnotationCanvas(store: annotations, stockCode: stockCode,
                                       tool: .pen, color: "accent", isEditing: false)
                    .allowsHitTesting(false)
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
            Chart(data.rows) { point in
                BarMark(x: .value("时间", point.time), y: .value("成交量", point.volume))
                    .foregroundStyle(AppStyle.accent.opacity(0.38))
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .frame(height: 70)
        }
        .padding(22).background(.white, in: RoundedRectangle(cornerRadius: 22))
    }
}

struct StockAnalyticsSections: View {
    @ObservedObject var model: AppModel
    let detail: StockResponse
    private let columns = [GridItem(.adaptive(minimum: 310), spacing: 18, alignment: .top)]

    var body: some View {
        LazyVGrid(columns: columns, alignment: .leading, spacing: 18) {
            if let technical = detail.technical { TechnicalCard(panel: technical) }
            if let fund = detail.fund { FundCard(panel: fund) }
            if let chips = detail.chips { ChipCard(panel: chips) }
            if let peers = detail.peers, !peers.isEmpty { PeersCard(peers: peers, model: model) }
            if let concepts = detail.concepts, !concepts.isEmpty { ConceptsCard(concepts: concepts) }
            if let announcements = detail.announcements, !announcements.isEmpty {
                AnnouncementsCard(items: announcements)
            }
        }
    }
}

private struct TechnicalCard: View {
    let panel: TechnicalPanel
    var body: some View {
        card(title: "技术指标", subtitle: "MACD · KDJ · 均线") {
            Chart {
                ForEach(panel.history) { point in
                    if let histogram = point.macdHist {
                        BarMark(x: .value("日期", point.tradeDate), y: .value("MACD", histogram))
                            .foregroundStyle(AppStyle.movement(histogram).opacity(0.42))
                    }
                    if let dif = point.macdDif {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("DIF", dif))
                            .foregroundStyle(AppStyle.accent)
                    }
                    if let dea = point.macdDea {
                        LineMark(x: .value("日期", point.tradeDate), y: .value("DEA", dea))
                            .foregroundStyle(.orange)
                    }
                }
            }
            .chartXAxis(.hidden).frame(height: 130)
            metricLine("MA5", panel.metrics.ma5, "MA10", panel.metrics.ma10)
            metricLine("MA20", panel.metrics.ma20, "MA60", panel.metrics.ma60)
            metricLine("K", panel.metrics.kdjK, "D", panel.metrics.kdjD)
        }
    }
}

private struct FundCard: View {
    let panel: FundPanel
    var body: some View {
        card(title: "资金动向", subtitle: "主力净流入") {
            Chart(panel.history) { point in
                if let value = point.latestMainInflow {
                    BarMark(x: .value("日期", point.tradeDate), y: .value("净流入", value))
                        .foregroundStyle(AppStyle.movement(value).opacity(0.7))
                }
            }
            .chartXAxis(.hidden).frame(height: 130)
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
            valueLine("平均成本", panel.average)
            valueLine("获利比例", panel.winnerRate.map { $0 * 100 }, suffix: "%")
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

private func valueLine(_ title: String, _ value: Double?, compact: Bool = false, suffix: String = "") -> some View {
    HStack {
        Text(title).foregroundStyle(.secondary)
        Spacer()
        Text(compact ? AppStyle.compact(value) : (value.map { String(format: "%.2f", $0) } ?? "—") + suffix)
            .monospacedDigit().foregroundStyle(AppStyle.movement(value))
    }
    .font(.caption)
}
