import CryptoKit
import Foundation

extension AppModel {
    /// Screen-space ink must not be projected onto a changed chart coordinate system.
    /// Values inside unchanged bounds, cursor movement and quote timestamps are omitted.
    var workspaceInkDomainID: String {
        let kinds = Set(workspace.layout.selectedPage?.visibleCards.map(\.kind) ?? [])
        let candles = displayedKlineCandles
        let range = linkedKlineRange
        let visibleCandles = range.compactMap { candles.indices.contains($0) ? candles[$0] : nil }
        let needsIndicators = kinds.contains(.macd) || kinds.contains(.kdj)
            || kinds.contains(.kline) || kinds.contains(.klineChips)
            || (kinds.contains(.chart) && chartPeriod != .intraday)
        let indicators = needsIndicators
            ? NativeChartIndicators.series(candles: candles, panel: klinePeriod == .day ? displayedDetail?.technical : nil)
            : []
        let visibleIndicators = NativeChartIndicators.visible(indicators, context: linkedKlineContext)
        var material = ["stock-ink-domains-v1"]
        for kind in kinds.sorted(by: { $0.rawValue < $1.rawValue }) {
            switch kind {
            case .chart where chartPeriod == .intraday, .intraday:
                material += [kind.rawValue] + StockInkDomainValues.intraday(displayedIntraday, range: linkedIntradayRange)
            case .chart, .kline, .klineChips:
                material += [kind.rawValue, klinePeriod.rawValue]
                    + StockInkDomainValues.candles(visibleCandles, indicators: visibleIndicators)
                if kind == .klineChips {
                    material += ["chip-x", StockInkDomainValues.number(StockInkDomainValues.chipMaximum(chipDistribution))]
                }
            case .macd:
                let values = visibleIndicators.flatMap { [$0.dif, $0.dea, $0.histogram].compactMap { $0 } }.filter(\.isFinite)
                // Swift Charts chooses tick rounding, but its automatic domain's inputs
                // are these extrema plus the visible zero-axis RuleMark.
                material += [kind.rawValue, klinePeriod.rawValue]
                    + StockInkDomainValues.times(visibleIndicators.map(\.time))
                    + StockInkDomainValues.bounds(min(0, values.min() ?? 0), max(0, values.max() ?? 0))
            case .kdj:
                let values = visibleIndicators.flatMap { [$0.k, $0.d, $0.j].compactMap { $0 } }.filter(\.isFinite)
                material += [kind.rawValue, klinePeriod.rawValue]
                    + StockInkDomainValues.times(visibleIndicators.map(\.time))
                    + StockInkDomainValues.bounds(min(0, (values.min() ?? 0) - 5), max(100, (values.max() ?? 100) + 5))
            case .chipDistribution:
                material += [kind.rawValue] + StockInkDomainValues.chips(chipDistribution)
            default:
                break
            }
        }
        // Length-prefix the fields so even unusual source date strings cannot collide.
        let input = material.map { "\($0.utf8.count):\($0)" }.joined()
        return SHA256.hash(data: Data(input.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

private enum StockInkDomainValues {
    static func number(_ value: Double) -> String {
        guard value.isFinite else { return "unavailable" }
        return value == 0 ? "0" : String(value)
    }

    static func bounds(_ lower: Double, _ upper: Double) -> [String] {
        [number(lower), number(upper)]
    }

    static func times(_ values: [String]) -> [String] {
        [values.first ?? "empty", values.last ?? "empty", String(values.count)]
    }

    static func candles(_ candles: [Candle], indicators: [NativeIndicatorPoint]) -> [String] {
        guard !candles.isEmpty else { return ["empty"] }
        let averages = indicators.flatMap { [$0.ma5, $0.ma10, $0.ma20].compactMap { $0 } }.filter(\.isFinite)
        let low = (candles.map(\.low).filter(\.isFinite) + averages).min() ?? 0
        let high = (candles.map(\.high).filter(\.isFinite) + averages).max() ?? 1
        let padding = max(max((high - low) * 0.10, high * 0.005), 0.01)
        let volumes = candles.compactMap(\.volume).filter(\.isFinite)
        return times(candles.map(\.time)) + bounds(low - padding, high + padding)
            + ["volume", number(min(0, volumes.min() ?? 0)), number(max(0, volumes.max() ?? 0))]
    }

    static func intraday(_ data: IntradayResponse?, range: Range<Int>) -> [String] {
        guard let data else { return ["unavailable"] }
        let points = data.rows.compactMap { point -> (slot: Int, point: IntradayPoint)? in
            guard let slot = StockTimelineTime.sessionSlot(point.time) else { return nil }
            return (slot, point)
        }.sorted { $0.slot < $1.slot }
        let visible = points.filter { range.contains($0.slot) }.map(\.point)
        let values = visible.flatMap { [$0.price, $0.averagePrice].compactMap { $0 } }.filter(\.isFinite)
        let low: Double
        let high: Double
        if values.isEmpty {
            let reference = data.previousClose.flatMap { $0.isFinite ? $0 : nil }
                ?? points.last?.point.price ?? 1
            let padding = max(abs(reference) * 0.005, 0.01)
            low = reference - padding; high = reference + padding
        } else {
            let minimum = values.min() ?? 0
            let maximum = values.max() ?? 1
            if range == 0..<242 {
                let reference = data.previousClose.flatMap { $0.isFinite ? $0 : nil } ?? (minimum + maximum) / 2
                let distance = max(abs(maximum - reference), abs(reference - minimum), reference * 0.005, 0.01)
                low = reference - distance * 1.08; high = reference + distance * 1.08
            } else {
                let padding = max((maximum - minimum) * 0.10, abs(maximum) * 0.001, 0.01)
                low = minimum - padding; high = maximum + padding
            }
        }
        let volumes = visible.map(\.volume).filter(\.isFinite)
        return [data.tradeDate, String(range.lowerBound), String(range.upperBound)]
            + times(visible.map(\.time)) + bounds(low, high)
            + ["volume", number(min(0, volumes.min() ?? 0)), number(max(0, volumes.max() ?? 0))]
    }

    static func chipMaximum(_ data: ChipDistributionResponse?) -> Double {
        max(chipRows(data).map(\.percent).max() ?? 1, 0.001)
    }

    static func chips(_ data: ChipDistributionResponse?) -> [String] {
        let rows = chipRows(data)
        guard !rows.isEmpty else { return ["empty"] }
        let low = rows.first?.price ?? 0
        let high = rows.last?.price ?? 1
        let padding = max((high - low) * 0.05, abs(high) * 0.001, 0.01)
        return bounds(low - padding, high + padding) + ["chip-x", number(chipMaximum(data))]
    }

    private static func chipRows(_ data: ChipDistributionResponse?) -> [ChipDistributionRow] {
        (data?.rows ?? []).filter { $0.price.isFinite && $0.percent.isFinite && $0.percent > 0 }
            .sorted { $0.price < $1.price }
    }
}
