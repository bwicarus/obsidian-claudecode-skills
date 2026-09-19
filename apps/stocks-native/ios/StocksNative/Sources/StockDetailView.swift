import Charts
import SwiftUI

struct StockDetailView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ScrollView {
            if let detail = model.detail {
                VStack(alignment: .leading, spacing: 26) {
                    header(detail.stock)
                    if let error = model.detailError {
                        Label(error, systemImage: "exclamationmark.circle")
                            .font(.footnote).foregroundStyle(.red)
                    }
                    CandleChart(candles: detail.candles,
                                stockCode: detail.stock.code,
                                annotations: model.annotations)
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("数据时间").font(.caption).foregroundStyle(.secondary)
                            Text(detail.asOf ?? "服务器未提供时间")
                                .font(.caption).monospacedDigit().textSelection(.enabled)
                        }
                        Spacer()
                        Text("来源 · VPS 股票数据库")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(28)
            } else if model.isLoadingDetail {
                ProgressView("读取股票信息…").padding(60)
            } else if let error = model.detailError {
                ContentUnavailableView {
                    Label("暂时无法读取", systemImage: "wifi.exclamationmark")
                } description: { Text(error) } actions: {
                    Button("重试") { Task { await model.loadDetail() } }.buttonStyle(.bordered)
                }
                .padding(.top, 80)
            } else {
                ContentUnavailableView("选择一只股票", systemImage: "chart.xyaxis.line", description: Text("查看行情、原生 K 线和语音助手。"))
                    .padding(.top, 80)
            }
        }
        .refreshable { await model.loadDetail() }
        .background(AppStyle.canvas)
    }

    private func header(_ stock: Stock) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(stock.code).font(.caption).monospaced().tracking(2).foregroundStyle(.secondary)
                    Text(stock.name).font(.system(size: 30, weight: .semibold, design: .rounded)).foregroundStyle(AppStyle.ink)
                }
                Spacer()
                if model.isLoadingDetail { ProgressView().controlSize(.small) }
                Button { Task { await model.loadDetail() } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless).disabled(model.isLoadingDetail)
                    .accessibilityLabel("刷新个股")
            }
            HStack(alignment: .firstTextBaseline, spacing: 16) {
                Text(AppStyle.price(stock.price)).font(.system(size: 42, weight: .medium, design: .rounded)).monospacedDigit()
                Text(AppStyle.change(stock.changePct))
                    .font(.system(.title3, design: .rounded, weight: .medium))
                    .foregroundStyle(AppStyle.movement(stock.changePct)).monospacedDigit()
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 24) { metrics(stock) }
                VStack(alignment: .leading, spacing: 10) { metrics(stock) }
            }
        }
    }

    @ViewBuilder private func metrics(_ stock: Stock) -> some View {
        if let sector = stock.sector, !sector.isEmpty {
            Label(sector, systemImage: "square.grid.2x2").font(.subheadline).foregroundStyle(.secondary)
        }
        if let turnover = stock.turnover {
            Text("成交额  \(turnover.formatted(.number.notation(.compactName).precision(.fractionLength(0...2))))")
                .font(.subheadline).foregroundStyle(.secondary)
        }
    }
}

private struct IndexedCandle: Identifiable {
    let id: Int
    let candle: Candle
    var x: Double { Double(id) }
    var left: Double { x - 0.31 }
    var right: Double { x + 0.31 }
    var bodyBottom: Double { min(candle.open, candle.close) }
    var bodyTop: Double {
        max(candle.open, candle.close) + (candle.open == candle.close ? 0.005 : 0)
    }
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
        RectangleMark(xStart: .value("开始", item.left),
                      xEnd: .value("结束", item.right),
                      yStart: .value("开盘", item.bodyBottom),
                      yEnd: .value("收盘", item.bodyTop))
            .foregroundStyle(item.color)
    }
}

private struct CandleChart: View {
    let candles: [Candle]
    let stockCode: String
    @ObservedObject var annotations: AnnotationStore
    @State private var visibleCount = 60
    @State private var selectedX: Double?
    @State private var annotationMode = false
    @State private var annotationTool: AnnotationTool = .pen
    @State private var annotationColor = "accent"
    @State private var confirmingClear = false

    private var visible: [IndexedCandle] {
        Array(candles.suffix(visibleCount)).enumerated().map { IndexedCandle(id: $0.offset, candle: $0.element) }
    }
    private var priceDomain: ClosedRange<Double> {
        let minimum = visible.map(\.candle.low).min() ?? 0
        let maximum = visible.map(\.candle.high).max() ?? 1
        let padding = max(max((maximum - minimum) * 0.10, maximum * 0.005), 0.01)
        return (minimum - padding)...(maximum + padding)
    }
    private var inspected: IndexedCandle? {
        guard !visible.isEmpty else { return nil }
        let index = selectedX.map { min(max(Int($0.rounded()), 0), visible.count - 1) } ?? visible.count - 1
        return visible[index]
    }
    private var tickPositions: [Double] {
        guard visible.count > 1 else { return [0] }
        return [0, Double((visible.count - 1) / 2), Double(visible.count - 1)]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            ViewThatFits(in: .horizontal) {
                HStack {
                    Text("价格走势").font(.headline)
                    Spacer(minLength: 16)
                    annotationToggle
                    periodPicker.frame(width: 220)
                }
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("价格走势").font(.headline)
                        Spacer()
                        annotationToggle
                    }
                    periodPicker
                }
            }
            if annotationMode { annotationToolbar }
            if visible.isEmpty {
                ContentUnavailableView("暂无 K 线数据", systemImage: "chart.bar.xaxis", description: Text("服务器尚未提供这只股票的历史行情。"))
                    .frame(height: 300)
            } else {
                if let item = inspected { candleSummary(item.candle) }
                ZStack {
                    Chart {
                        ForEach(visible) { item in
                            CandlePriceMarks(item: item)
                        }
                        if let selectedX {
                            RuleMark(x: .value("选中", selectedX.rounded()))
                                .foregroundStyle(AppStyle.ink.opacity(0.25))
                                .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                        }
                    }
                    .chartXScale(domain: -1...Double(visible.count))
                    .chartYScale(domain: priceDomain)
                    .chartXSelection(value: $selectedX)
                    .chartYAxis {
                        AxisMarks(position: .trailing, values: .automatic(desiredCount: 5)) {
                            AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(Color.gray.opacity(0.13))
                            AxisValueLabel().foregroundStyle(.secondary)
                        }
                    }
                    .chartXAxis {
                        AxisMarks(values: tickPositions) { value in
                            AxisValueLabel {
                                if let x = value.as(Double.self), visible.indices.contains(Int(x)) {
                                    Text(shortDate(visible[Int(x)].candle.time)).font(.caption2)
                                }
                            }
                        }
                    }
                    NativeAnnotationCanvas(store: annotations, stockCode: stockCode,
                                           tool: annotationTool, color: annotationColor,
                                           isEditing: annotationMode)
                        .allowsHitTesting(annotationMode)
                }
                .frame(height: 290)
                .accessibilityLabel("原生蜡烛图，\(visible.count) 根日 K 线。拖动查看开盘、最高、最低、收盘。")
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
                .chartXScale(domain: -1...Double(visible.count))
                .chartXAxis(.hidden)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 2)) { axisValue in
                        AxisValueLabel {
                            if let value = axisValue.as(Double.self) {
                                Text(value.formatted(.number.notation(.compactName)))
                            }
                        }
                    }
                }
                .frame(height: 85)
                Text(annotationMode ? "标注模式：使用手指或 Apple Pencil 绘制。标注按股票保存在本机。" : "触碰并拖动图表查看当日数据。红色上涨，绿色下跌。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(22)
        .background(.white, in: RoundedRectangle(cornerRadius: 22))
        .onChange(of: visibleCount) { _, _ in selectedX = nil }
        .onChange(of: candles.first?.time) { _, _ in selectedX = nil }
        .onChange(of: stockCode) { _, _ in annotationMode = false; selectedX = nil }
        .confirmationDialog("清除此股票的全部标注？", isPresented: $confirmingClear, titleVisibility: .visible) {
            Button("清除全部", role: .destructive) { _ = annotations.clear(stockCode: stockCode) }
            Button("取消", role: .cancel) { }
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

    private var periodPicker: some View {
        Picker("显示交易日", selection: $visibleCount) {
            Text("30 日").tag(30)
            Text("60 日").tag(60)
            Text("120 日").tag(120)
        }.pickerStyle(.segmented)
    }

    private func candleSummary(_ candle: Candle) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(shortDate(candle.time)).font(.caption).foregroundStyle(.secondary)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) { candleValues(candle) }
                VStack(alignment: .leading, spacing: 6) { candleValues(candle) }
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

    private func shortDate(_ time: String) -> String { String(time.prefix(10)) }
}
