import Charts
import SwiftUI

struct StockDetailView: View {
    @ObservedObject var model: AppModel
    @ObservedObject var selection: StockSelectionModel

    var body: some View {
        Group {
            if let detail = model.displayedDetail {
                let currentStock = model.displayedStock ?? detail.stock
                VStack(spacing: 0) {
                    quoteHeader(currentStock, sector: currentStock.sector ?? detail.stock.sector, asOf: detail.asOf)
                    StockTimelineBar(model: model)
                    if let error = model.detailError {
                        Label(error, systemImage: "exclamationmark.circle")
                            .font(.footnote).foregroundStyle(.red)
                            .padding(.horizontal, 22).padding(.bottom, 8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    Divider()
                    StockWorkspaceView(model: model, detail: detail)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else if model.isLoadingDetail {
                ProgressView("读取股票信息…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error = model.detailError {
                ContentUnavailableView {
                    Label("暂时无法读取", systemImage: "wifi.exclamationmark")
                } description: { Text(error) } actions: {
                    Button("重试") { Task { await model.loadDetail() } }.buttonStyle(.bordered)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ContentUnavailableView("选择一只股票", systemImage: "chart.xyaxis.line", description: Text("查看行情、原生 K 线和语音助手。"))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(AppStyle.canvas)
    }

    private func quoteHeader(_ stock: Stock, sector: String?, asOf: String?) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 18) {
                stockIdentity(stock, sector: sector, asOf: asOf)
                Spacer(minLength: 12)
                priceBlock(stock)
                refreshButton
            }
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top) {
                    stockIdentity(stock, sector: sector, asOf: asOf)
                    Spacer()
                    refreshButton
                }
                priceBlock(stock)
            }
        }
        .padding(.horizontal, 22).padding(.vertical, 15)
        .background(.white)
    }

    private func stockIdentity(_ stock: Stock, sector: String?, asOf: String?) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                StockFavoriteButton(model: selection, code: stock.code, name: stock.name)
                Text(stock.name)
                    .font(.system(.title2, design: .rounded, weight: .semibold))
                    .foregroundStyle(AppStyle.ink).lineLimit(1)
                if let sector, !sector.isEmpty {
                    Text(sector)
                        .font(.caption2.weight(.medium)).lineLimit(1)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .background(AppStyle.canvas, in: Capsule())
                }
            }
            HStack(spacing: 8) {
                Text(stock.code).monospaced().tracking(1.2)
                Text(asOf.map { "资料 \($0)" } ?? "等待资料时间").lineLimit(1)
            }
            .font(.caption2).foregroundStyle(.secondary)
        }
    }

    private func priceBlock(_ stock: Stock) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(AppStyle.price(stock.price))
                .font(.system(size: 36, weight: .medium, design: .rounded))
                .monospacedDigit()
            VStack(alignment: .trailing, spacing: 3) {
                Text(AppStyle.change(stock.changePct))
                Text(stock.changeAmount.map { String(format: "%+.2f", $0) } ?? "—")
            }
            .font(.subheadline.weight(.medium)).monospacedDigit()
            .foregroundStyle(AppStyle.movement(stock.changePct))
        }
    }

    private var refreshButton: some View {
        HStack(spacing: 8) {
            if model.isLoadingDetail { ProgressView().controlSize(.small) }
            Button { Task { await refreshDetail() } } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).disabled(model.isLoadingDetail)
                .accessibilityLabel("刷新个股")
        }
    }

    private func refreshDetail() async {
        await model.loadDetail()
        await model.loadRealtime()
        await model.loadChart()
    }
}

private struct IndexedCandle: Identifiable {
    let id: Int
    let candle: Candle
    var x: Double { Double(id) }
    var left: Double { x - 0.31 }
    var right: Double { x + 0.31 }
    var bodyBottom: Double { min(candle.open, candle.close) }
    var bodyTop: Double { max(candle.open, candle.close) }
    var color: Color { AppStyle.movement(candle.close - candle.open) }
}

private struct CandlePriceMarks: ChartContent {
    let item: IndexedCandle
    var body: some ChartContent {
        RuleMark(x: .value("交易日", item.x),
                 yStart: .value("最低", item.candle.low),
                 yEnd: .value("最高", item.candle.high))
            .foregroundStyle(item.color)
            .lineStyle(StrokeStyle(lineWidth: 1))
        if item.candle.open == item.candle.close {
            RuleMark(xStart: .value("开始", item.left), xEnd: .value("结束", item.right),
                     y: .value("开收盘", item.candle.close))
                .foregroundStyle(item.color)
                .lineStyle(StrokeStyle(lineWidth: 1.5))
        } else {
            RectangleMark(xStart: .value("开始", item.left),
                          xEnd: .value("结束", item.right),
                          yStart: .value("开盘", item.bodyBottom),
                          yEnd: .value("收盘", item.bodyTop))
                .foregroundStyle(item.color)
        }
    }
}

struct CandleChart: View {
    let candles: [Candle]
    let stockCode: String
    let period: ChartPeriod
    @ObservedObject var annotations: AnnotationStore
    let onContextChange: (VoiceChartSnapshot) async -> Void
    var chipDistribution: ChipDistributionResponse? = nil
    var showsChips = false
    var externalContext: VoiceChartContext? = nil
    var sharedRange: Range<Int>? = nil
    var contextSuppressed = false
    var currentPrice: Double? = nil
    @State private var pricePlotFrame: CGRect = .zero
    @State private var selectedTime: String?
    @State private var annotationMode = false
    @State private var annotationTool: AnnotationTool = .pen
    @State private var annotationColor = "accent"
    @State private var confirmingClear = false

    private var visibleRange: Range<Int> {
        guard !candles.isEmpty else { return 0..<0 }
        if let sharedRange {
            return max(0, min(candles.count, sharedRange.lowerBound))..<max(0, min(candles.count, sharedRange.upperBound))
        }
        let requested = max(0, candles.count - 60)..<candles.count
        let lower = min(max(requested.lowerBound, 0), candles.count - 1)
        let upper = min(max(requested.upperBound, lower + 1), candles.count)
        return lower..<upper
    }
    private var visible: [IndexedCandle] {
        visibleRange.map { IndexedCandle(id: $0, candle: candles[$0]) }
    }
    private var indicators: [NativeIndicatorPoint] { NativeChartIndicators.calculate(candles) }
    private var visibleIndicators: [NativeIndicatorPoint] { indicators.filter { visibleRange.contains($0.id) } }
    private var inspectedIndicator: NativeIndicatorPoint? {
        indicators.first { $0.id == inspected?.id }
    }
    private var priceDomain: ClosedRange<Double> {
        let averages = visibleIndicators.flatMap { [$0.ma5, $0.ma10, $0.ma20].compactMap { $0 } }
        let minimum = (visible.map(\.candle.low) + averages).min() ?? 0
        let maximum = (visible.map(\.candle.high) + averages).max() ?? 1
        let padding = max(max((maximum - minimum) * 0.10, maximum * 0.005), 0.01)
        return (minimum - padding)...(maximum + padding)
    }
    private var inspected: IndexedCandle? {
        selectedCandle ?? visible.last
    }
    private var selectedCandle: IndexedCandle? {
        guard let selectedTime else { return nil }
        return visible.first { $0.candle.time == selectedTime }
    }
    private var selection: Binding<Double?> {
        Binding(get: { selectedCandle?.x }, set: { value in
            guard let value, value.isFinite, !visible.isEmpty else { selectedTime = nil; return }
            let index = Int(min(max(value.rounded(), Double(visibleRange.lowerBound)), Double(visibleRange.upperBound - 1)))
            selectedTime = candles[index].time
        })
    }
    private var xDomain: ClosedRange<Double> {
        (Double(visibleRange.lowerBound) - 0.6)...max(0.6, Double(visibleRange.upperBound) - 0.4)
    }
    private var tickPositions: [Double] {
        guard visible.count > 1 else { return [Double(visibleRange.lowerBound)] }
        return [Double(visibleRange.lowerBound), Double((visibleRange.lowerBound + visibleRange.upperBound - 1) / 2), Double(visibleRange.upperBound - 1)]
    }

    private var annotationViewport: ChartAnnotationViewport {
        ChartAnnotationViewport(period: period.rawValue, times: candles.map(\.time),
                                xDomain: xDomain, yDomain: priceDomain)
    }

    private var voiceContextSnapshot: VoiceChartSnapshot {
        let point = inspected.map {
            VoiceChartPoint(time: $0.candle.time, open: $0.candle.open, high: $0.candle.high,
                            low: $0.candle.low, close: $0.candle.close, volume: $0.candle.volume)
        }
        let chart = VoiceChartContext(stockCode: stockCode, period: period.rawValue, kind: "candles",
                                      firstVisibleTime: visible.first?.candle.time,
                                      lastVisibleTime: visible.last?.candle.time,
                                      visiblePointCount: visible.count, selectedPoint: point,
                                      selectionSource: selectedCandle == nil ? (visibleRange.upperBound == candles.count ? "latest" : "visible_end") : "cursor")
        return VoiceChartSnapshot(chart: chart,
                                  annotations: annotations.voiceContext(stockCode: stockCode,
                                                                        editing: annotationMode, tool: annotationTool,
                                                                        viewport: annotationViewport))
    }

    var body: some View {
        ChartCardViewport { height in
            chartContent(contentHeight: height)
        }
        .task(id: contextSuppressed ? nil : voiceContextSnapshot) {
            if !contextSuppressed { await onContextChange(voiceContextSnapshot) }
        }
        .onChange(of: externalContext) { _, context in
            guard let context, context.stockCode == stockCode, context.period == period.rawValue else { return }
            let nextSelection = context.selectionSource == "cursor" ? context.selectedPoint?.time : nil
            if selectedTime != nextSelection { selectedTime = nextSelection }
        }
        .onChange(of: visible.map(\.candle.time)) { _, times in
            if let selectedTime, !times.contains(selectedTime) { self.selectedTime = nil }
        }
        .onChange(of: stockCode) { _, _ in annotationMode = false; selectedTime = nil }
        .confirmationDialog("清除此股票的全部标注？", isPresented: $confirmingClear, titleVisibility: .visible) {
            Button("清除全部", role: .destructive) { _ = annotations.clear(stockCode: stockCode) }
            Button("取消", role: .cancel) { }
        }
    }

    private func chartContent(contentHeight: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("价格走势").font(.headline)
                Spacer(minLength: 12)
                annotationToggle
            }
            if annotationMode { annotationToolbar }
            if visible.isEmpty {
                ContentUnavailableView("所选范围暂无 K 线", systemImage: "chart.bar.xaxis", description: Text("当前已载入的数据没有覆盖这个时间范围。"))
                    .frame(maxHeight: .infinity)
            } else {
                if let item = inspected { candleSummary(item.candle) }
                movingAverageLegend
                if contentHeight > 0 {
                    GeometryReader { geometry in
                        priceAndChips(height: geometry.size.height)
                    }
                    .frame(minHeight: 168)
                } else {
                    priceAndChips(height: 403)
                }
                if showsChips, let chipDistribution, !chipDistribution.rows.isEmpty {
                    ChipDistributionSummary(data: chipDistribution, currentPrice: currentPrice ?? chipDistribution.currentPrice ?? candles.last?.close)
                }
                if annotations.legacyAnnotationCount(for: stockCode) > 0 {
                    Text("旧版笔迹仍保留在本机；因缺少行情坐标，暂不叠加显示。")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                Text(annotationMode ? "使用手指或 Apple Pencil 绘制；新标注随行情缩放和平移。" : "拖动图表查看单根数据；拖动下方范围条调整视野。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .frame(height: contentHeight > 0 ? contentHeight : nil, alignment: .top)
    }

    private func priceAndChips(height: CGFloat) -> some View {
        HStack(alignment: .top, spacing: 12) {
            priceAndVolumeCharts(height: height).frame(maxWidth: .infinity)
            if showsChips {
                ChipDistributionPlot(data: chipDistribution, currentPrice: currentPrice ?? chipDistribution?.currentPrice ?? candles.last?.close,
                                     priceDomain: priceDomain, compact: true)
                    .frame(width: 110, height: pricePlotFrame.height > 0 ? pricePlotFrame.height : max(100, height * 0.7))
                    .padding(.top, pricePlotFrame.minY)
            }
        }
    }

    private var movingAverageLegend: some View {
        HStack(spacing: 16) {
            averageValue("MA5", inspectedIndicator?.ma5, color: .orange)
            averageValue("MA10", inspectedIndicator?.ma10, color: .blue)
            averageValue("MA20", inspectedIndicator?.ma20, color: .purple)
        }
        .font(.caption).monospacedDigit()
    }

    private func averageValue(_ title: String, _ value: Double?, color: Color) -> some View {
        Text("\(title) \(AppStyle.price(value))").foregroundStyle(color).lineLimit(1).minimumScaleFactor(0.7)
    }

    private func priceAndVolumeCharts(height: CGFloat) -> some View {
        let volumeHeight = min(100, max(40, (height - 28) * 0.22))
        let priceHeight = max(100, height - volumeHeight - 28)
        return VStack(alignment: .leading, spacing: 6) {
                ZStack {
                    Chart {
                        ForEach(visible) { item in
                            CandlePriceMarks(item: item)
                        }
                        ForEach(visibleIndicators) { point in
                            if let ma5 = point.ma5 {
                                LineMark(x: .value("时间", Double(point.id)), y: .value("MA5", ma5), series: .value("均线", "MA5"))
                                    .foregroundStyle(Color.orange).lineStyle(StrokeStyle(lineWidth: 1.2))
                            }
                            if let ma10 = point.ma10 {
                                LineMark(x: .value("时间", Double(point.id)), y: .value("MA10", ma10), series: .value("均线", "MA10"))
                                    .foregroundStyle(Color.blue).lineStyle(StrokeStyle(lineWidth: 1.2))
                            }
                            if let ma20 = point.ma20 {
                                LineMark(x: .value("时间", Double(point.id)), y: .value("MA20", ma20), series: .value("均线", "MA20"))
                                    .foregroundStyle(Color.purple).lineStyle(StrokeStyle(lineWidth: 1.2))
                            }
                        }
                        if let selectedCandle {
                            RuleMark(x: .value("选中", selectedCandle.x))
                                .foregroundStyle(AppStyle.ink.opacity(0.25))
                                .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                        }
                    }
                    .chartXScale(domain: xDomain, range: .plotDimension(padding: 0))
                    .chartYScale(domain: priceDomain)
                    .chartPlotStyle { $0.clipped() }.chartLegend(.hidden)
                    .chartXSelection(value: selection)
                    .chartYAxis {
                        AxisMarks(position: .trailing, values: .automatic(desiredCount: 5)) { value in
                            AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(Color.gray.opacity(0.13))
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
                                if let x = value.as(Double.self), candles.indices.contains(Int(x)) {
                                    Text(shortDate(candles[Int(x)].time)).font(.caption2)
                                }
                            }
                        }
                    }
                    .chartOverlay { proxy in
                        GeometryReader { geometry in
                            if let plotFrame = proxy.plotFrame {
                                let frame = geometry[plotFrame]
                                Color.clear.onAppear { pricePlotFrame = frame }
                                    .onChange(of: frame) { _, updated in pricePlotFrame = updated }
                                NativeAnnotationCanvas(store: annotations, stockCode: stockCode,
                                                       tool: annotationTool, color: annotationColor,
                                                       isEditing: annotationMode, viewport: annotationViewport)
                                    .frame(width: frame.width, height: frame.height)
                                    .clipped()
                                    .offset(x: frame.minX, y: frame.minY)
                            }
                        }
                        .allowsHitTesting(annotationMode)
                    }
                }
                .frame(height: priceHeight)
                .accessibilityLabel("原生蜡烛图，\(visible.count) 根 K 线。拖动查看开盘、最高、最低、收盘。")
                HStack {
                    Text("成交量").font(.caption).foregroundStyle(.secondary)
                    if let volume = inspected?.candle.volume {
                        Text(volume.formatted(.number.notation(.compactName).precision(.fractionLength(0...2))))
                            .font(.caption).monospacedDigit().foregroundStyle(.secondary)
                    } else {
                        Text("暂无数据").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Chart(visible) { item in
                    if let volume = item.candle.volume {
                        BarMark(x: .value("交易日", Double(item.id)), y: .value("成交量", volume), width: .ratio(0.65))
                            .foregroundStyle(AppStyle.movement(item.candle.close - item.candle.open).opacity(0.55))
                    }
                }
                .chartXScale(domain: xDomain, range: .plotDimension(padding: 0))
                .chartXAxis(.hidden)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 2)) { axisValue in
                        AxisValueLabel {
                            if let value = axisValue.as(Double.self) {
                                Text(value.formatted(.number.notation(.compactName)))
                                    .font(.caption2).monospacedDigit()
                                    .frame(width: 52, alignment: .trailing)
                            }
                        }
                    }
                }
                .frame(height: volumeHeight)
        }
    }

    private var annotationToggle: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.18)) { annotationMode.toggle() }
        } label: {
            Label(annotationMode ? "完成" : "标记", systemImage: annotationMode ? "checkmark" : "pencil.and.scribble")
        }
        .buttonStyle(.bordered)
        .tint(annotationMode ? AppStyle.accent : .secondary)
        .controlSize(.small)
    }

    private var annotationToolbar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(AnnotationTool.allCases) { tool in
                    Button { annotationTool = tool } label: {
                        Label(tool.title, systemImage: tool.symbol)
                            .labelStyle(.iconOnly)
                            .frame(width: 24, height: 24)
                    }
                    .buttonStyle(.bordered)
                    .tint(annotationTool == tool ? AppStyle.accent : .secondary)
                    .accessibilityLabel(tool.title)
                }
                Divider().frame(height: 24)
                Menu {
                    colorButton("墨绿", value: "accent")
                    colorButton("红色", value: "red")
                    colorButton("橙色", value: "orange")
                    colorButton("蓝色", value: "blue")
                } label: {
                    Image(systemName: "circle.fill").foregroundStyle(annotationTint)
                        .frame(width: 32, height: 32)
                }
                .accessibilityLabel("标注颜色")
                Button { _ = annotations.undo(stockCode: stockCode) } label: { Image(systemName: "arrow.uturn.backward") }
                    .buttonStyle(.bordered)
                    .disabled(annotations.annotations(for: stockCode).isEmpty)
                    .accessibilityLabel("撤销上一条标注")
                Button(role: .destructive) { confirmingClear = true } label: { Image(systemName: "trash") }
                    .buttonStyle(.bordered)
                    .disabled(annotations.annotations(for: stockCode).isEmpty)
                    .accessibilityLabel("清除全部标注")
            }
        }
    }

    @ViewBuilder private func colorButton(_ title: String, value: String) -> some View {
        Button {
            annotationColor = value
        } label: {
            if annotationColor == value { Label(title, systemImage: "checkmark") }
            else { Text(title) }
        }
    }

    private var annotationTint: Color {
        switch annotationColor {
        case "red": return AppStyle.up
        case "orange": return .orange
        case "blue": return .blue
        default: return AppStyle.accent
        }
    }

    private func candleSummary(_ candle: Candle) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(shortDate(candle.time)).font(.caption).foregroundStyle(.secondary)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) { candleValues(candle) }
                LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading), GridItem(.flexible(), alignment: .leading)],
                          alignment: .leading, spacing: 4) { candleValues(candle) }
            }
            .font(.caption).monospacedDigit().foregroundStyle(AppStyle.ink)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func candleValues(_ candle: Candle) -> some View {
        Text("开 \(AppStyle.price(candle.open))")
        Text("高 \(AppStyle.price(candle.high))")
        Text("低 \(AppStyle.price(candle.low))")
        Text("收 \(AppStyle.price(candle.close))")
    }

    private func shortDate(_ time: String) -> String {
        let normalized = time.replacingOccurrences(of: "T", with: " ")
        let parts = normalized.split(separator: " ")
        if parts.count >= 2, parts[1].contains(":") {
            return "\(parts[0].suffix(5)) \(parts[1].prefix(5))"
        }
        return String(normalized.prefix(10))
    }
}
