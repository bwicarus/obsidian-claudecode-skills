import SwiftUI

enum StockTableColumn: String, CaseIterable, Identifiable {
    case identity, price, changePct, volumeRatio, turnoverRate, turnover, marketCap
    case monitoring, strategy, sector, score, amplitude, volume, open, high, low, prevClose, peDynamic, pb
    case changeAmount, floatMarketCap, speed, change5m, change60d, changeYtd, upLimit, downLimit

    var id: String { rawValue }
    var title: String {
        switch self {
        case .identity: "名称 / 代码"
        case .price: "现价"
        case .changePct: "涨跌幅"
        case .volumeRatio: "量比"
        case .turnoverRate: "换手率"
        case .turnover: "成交额"
        case .marketCap: "总市值"
        case .monitoring: "盯盘"
        case .strategy: "AI 策略"
        case .sector: "行业"
        case .score: "评分"
        case .amplitude: "振幅"
        case .volume: "成交量"
        case .open: "今开"
        case .high: "最高"
        case .low: "最低"
        case .prevClose: "昨收"
        case .peDynamic: "市盈率"
        case .pb: "市净率"
        case .changeAmount: "涨跌额"
        case .floatMarketCap: "流通市值"
        case .speed: "涨速"
        case .change5m: "5 分钟涨幅"
        case .change60d: "60 日涨幅"
        case .changeYtd: "年初至今"
        case .upLimit: "涨停价"
        case .downLimit: "跌停价"
        }
    }
    var width: CGFloat {
        switch self {
        case .identity: 142
        case .sector: 158
        case .monitoring: 104
        case .strategy: 132
        case .turnover, .marketCap, .floatMarketCap, .volume, .change5m, .change60d, .changeYtd: 98
        case .changePct, .turnoverRate, .amplitude: 84
        default: 76
        }
    }
    var alignment: Alignment {
        self == .identity || self == .sector ? .leading : (self == .monitoring || self == .strategy) ? .center : .trailing
    }
    var selectionSortKey: String? {
        switch self {
        case .identity: "code"
        case .price, .changePct, .turnover, .turnoverRate, .marketCap, .score: rawValue
        default: nil
        }
    }
    static let defaults: [Self] = [.price, .changePct, .strategy, .volumeRatio, .turnoverRate, .turnover, .marketCap, .monitoring]
    static let defaultPreference = defaults.map(\.rawValue).joined(separator: ",")

    static func available(in context: StockSelectionSection) -> [Self] {
        context == .market ? allCases.filter { $0 != .score }
            : [.identity, .price, .changePct, .volumeRatio, .turnoverRate, .turnover, .marketCap,
               .monitoring, .strategy, .sector, .score, .amplitude]
    }

    static func visible(_ preference: String) -> [Self] {
        var seen: Set<Self> = [.identity]
        return [.identity] + preference.split(separator: ",").compactMap { raw in
            guard let column = Self(rawValue: String(raw)), seen.insert(column).inserted else { return nil }
            return column
        }
    }
}

/// The header and list share one horizontal scroll view, so every value stays under its label.
struct StockTable<Content: View>: View {
    let context: StockSelectionSection
    var sortKey: String? = nil
    var descending = false
    var sortingDisabled = false
    var onSort: ((String) -> Void)? = nil
    let content: ([StockTableColumn]) -> Content
    @AppStorage private var preference: String
    @State private var showingColumns = false

    init(context: StockSelectionSection, sortKey: String? = nil, descending: Bool = false,
         sortingDisabled: Bool = false, onSort: ((String) -> Void)? = nil,
         @ViewBuilder content: @escaping ([StockTableColumn]) -> Content) {
        self.context = context; self.sortKey = sortKey; self.descending = descending
        self.sortingDisabled = sortingDisabled; self.onSort = onSort; self.content = content
        _preference = AppStorage(wrappedValue: StockTableColumn.defaultPreference,
                                "stocksNative.stockTable.columns.v1.\(context.rawValue)")
    }

    private var columns: [StockTableColumn] {
        StockTableColumn.visible(preference).filter { StockTableColumn.available(in: context).contains($0) }
    }

    var body: some View {
        GeometryReader { geometry in
            let width = max(geometry.size.width, columns.reduce(CGFloat(88)) { $0 + $1.width })
            ScrollView(.horizontal) {
                VStack(spacing: 0) {
                    header.frame(width: width, height: 30, alignment: .leading)
                    Divider()
                    content(columns)
                        .listStyle(.plain)
                        .environment(\.defaultMinListRowHeight, 42)
                        .frame(width: width, height: max(1, geometry.size.height - 31))
                }
                .frame(width: width, height: geometry.size.height)
            }
            .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
            .overlay(alignment: .topTrailing) {
                Button { showingColumns = true } label: {
                    Image(systemName: "slider.horizontal.3")
                        .font(.caption).frame(width: 32, height: 30)
                        .background(AppStyle.canvas)
                }
                .buttonStyle(.plain).accessibilityLabel("自定义列表显示列")
            }
            .clipped()
        }
        .sheet(isPresented: $showingColumns) { StockTableColumnsEditor(context: context, preference: $preference) }
        .onAppear {
            let key = "stocksNative.stockTable.strategyColumn.v1.\(context.rawValue)"
            guard !UserDefaults.standard.bool(forKey: key) else { return }
            var values = StockTableColumn.visible(preference).filter { $0 != .identity }
            if !values.contains(.strategy) {
                let position = values.firstIndex(of: .changePct).map { $0 + 1 } ?? 0
                values.insert(.strategy, at: position)
                preference = values.map(\.rawValue).joined(separator: ",")
            }
            UserDefaults.standard.set(true, forKey: key)
        }
    }

    private var header: some View {
        HStack(spacing: 0) {
            Image(systemName: "star").frame(width: 32).accessibilityLabel("收藏")
            ForEach(columns) { column in
                Group {
                    if let onSort, let key = column.selectionSortKey {
                        Button { onSort(key) } label: {
                            HStack(spacing: 3) {
                                Text(column.title)
                                if sortKey == key { Image(systemName: descending ? "chevron.down" : "chevron.up").font(.system(size: 8, weight: .semibold)) }
                            }.frame(maxWidth: .infinity, alignment: column.alignment)
                        }.buttonStyle(.plain).disabled(sortingDisabled)
                    } else {
                        Text(column.title).frame(maxWidth: .infinity, alignment: column.alignment)
                    }
                }
                .padding(.horizontal, 6).frame(width: column.width, alignment: column.alignment)
            }
            Spacer(minLength: 0)
        }
        .font(.caption2).foregroundStyle(.secondary)
        .padding(.horizontal, 12).background(AppStyle.canvas)
    }
}

private struct StockTableColumnsEditor: View {
    let context: StockSelectionSection
    @Binding var preference: String
    @Environment(\.dismiss) private var dismiss
    private var visible: [StockTableColumn] {
        StockTableColumn.visible(preference).filter { $0 != .identity && StockTableColumn.available(in: context).contains($0) }
    }
    private var hidden: [StockTableColumn] {
        StockTableColumn.available(in: context).filter { $0 != .identity && !visible.contains($0) }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("名称、代码和收藏始终显示。\(context.title)的显示列单独记住。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("显示的列 · 拖动调整顺序") {
                    ForEach(visible) { column in
                        HStack {
                            Button { save(visible.filter { $0 != column }) } label: {
                                Image(systemName: "minus.circle.fill").foregroundStyle(.red)
                            }.buttonStyle(.borderless).accessibilityLabel("隐藏\(column.title)")
                            Text(column.title)
                        }
                    }.onMove { source, target in
                        var values = visible
                        values.move(fromOffsets: source, toOffset: target)
                        save(values)
                    }
                }
                if !hidden.isEmpty {
                    Section("可添加的列") {
                        ForEach(hidden) { column in
                            Button { save(visible + [column]) } label: {
                                Label(column.title, systemImage: "plus.circle")
                            }.buttonStyle(.borderless)
                        }
                    }
                }
                Section {
                    Button("恢复默认列") { preference = StockTableColumn.defaultPreference }
                    Text("当前数据源未提供的字段显示为 —。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .environment(\.editMode, .constant(.active))
            .navigationTitle("\(context.title)列表显示").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
        }
    }

    private func save(_ values: [StockTableColumn]) { preference = values.map(\.rawValue).joined(separator: ",") }
}

struct StockTableRecord {
    let code: String
    let name: String
    let sector: String?
    let values: [StockTableColumn: Double]

    init(_ stock: Stock) {
        code = stock.code; name = stock.name; sector = stock.sector
        let fields: [StockTableColumn: Double?] = [.price: stock.price, .changePct: stock.changePct, .volumeRatio: stock.volumeRatio,
                  .turnoverRate: stock.turnoverRate, .turnover: stock.turnover, .marketCap: stock.marketCap,
                  .amplitude: stock.amplitude, .volume: stock.volume, .open: stock.open, .high: stock.high,
                  .low: stock.low, .prevClose: stock.prevClose, .peDynamic: stock.peDynamic, .pb: stock.pb,
                  .changeAmount: stock.changeAmount, .floatMarketCap: stock.floatMarketCap, .speed: stock.speed,
                  .change5m: stock.change5m, .change60d: stock.change60d, .changeYtd: stock.changeYtd,
                  .upLimit: stock.upLimit, .downLimit: stock.downLimit]
        values = fields.compactMapValues { $0 }
    }

    init(_ stock: SelectionStock) {
        code = stock.code; name = stock.name; sector = stock.sector
        let fields: [StockTableColumn: Double?] = [.price: stock.price, .changePct: stock.changePct, .volumeRatio: stock.volumeRatio,
                  .turnoverRate: stock.turnoverRate, .turnover: stock.turnover, .marketCap: stock.marketCap,
                  .amplitude: stock.amplitude, .score: stock.score]
        values = fields.compactMapValues { $0 }
    }

    func text(_ column: StockTableColumn) -> String {
        if column == .sector { return sector.flatMap { $0.isEmpty ? nil : $0 } ?? "—" }
        let value = values[column]
        switch column {
        case .changePct, .speed, .change5m, .change60d, .changeYtd: return AppStyle.change(value)
        case .turnoverRate, .amplitude: return AppStyle.percent(value)
        case .turnover, .marketCap, .floatMarketCap, .volume: return AppStyle.compact(value)
        case .score: return value?.formatted(.number.precision(.fractionLength(0...1))) ?? "—"
        default: return AppStyle.price(value)
        }
    }
}
