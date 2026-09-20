import Foundation

struct NativeIndicatorPoint: Identifiable {
    let id: Int
    let time: String
    let ma5: Double?
    let ma10: Double?
    let ma20: Double?
    let dif: Double?
    let dea: Double?
    let histogram: Double?
    let k: Double?
    let d: Double?
    var j: Double? {
        guard let k, let d else { return nil }
        return 3 * k - 2 * d
    }
}

enum NativeChartIndicators {
    /// Use the full loaded series; changing the viewport must not reset EMA or KDJ.
    static func calculate(_ candles: [Candle]) -> [NativeIndicatorPoint] {
        guard let first = candles.first else { return [] }
        var ema12 = first.close
        var ema26 = first.close
        var dea = 0.0
        var k = 50.0
        var d = 50.0
        var sums = [5: 0.0, 10: 0.0, 20: 0.0]
        return candles.enumerated().map { index, candle in
            for length in [5, 10, 20] {
                sums[length, default: 0] += candle.close
                if index >= length { sums[length, default: 0] -= candles[index - length].close }
            }
            ema12 += (candle.close - ema12) * (2.0 / 13.0)
            ema26 += (candle.close - ema26) * (2.0 / 27.0)
            let dif = ema12 - ema26
            dea += (dif - dea) * (2.0 / 10.0)
            var displayedK: Double?
            var displayedD: Double?
            if index >= 8 {
                let window = candles[(index - 8)...index]
                let low = window.map(\.low).min() ?? candle.low
                let high = window.map(\.high).max() ?? candle.high
                let rsv = high == low ? 50.0 : (candle.close - low) / (high - low) * 100
                k = k * (2.0 / 3.0) + rsv / 3
                displayedK = (k * 100).rounded() / 100
                d = d * (2.0 / 3.0) + (displayedK ?? k) / 3
                displayedD = (d * 100).rounded() / 100
            }
            func ma(_ length: Int) -> Double? {
                index + 1 >= length ? sums[length, default: 0] / Double(length) : nil
            }
            return NativeIndicatorPoint(id: index, time: candle.time,
                                        ma5: ma(5), ma10: ma(10), ma20: ma(20),
                                        dif: dif, dea: dea, histogram: (2 * (dif - dea) * 1000).rounded() / 1000,
                                        k: displayedK, d: displayedD)
        }
    }

    static func series(candles: [Candle], panel: TechnicalPanel?) -> [NativeIndicatorPoint] {
        if !candles.isEmpty { return calculate(candles) }
        return (panel?.history ?? []).enumerated().map { index, point in
            NativeIndicatorPoint(id: index, time: point.tradeDate, ma5: nil, ma10: nil, ma20: nil,
                                 dif: point.macdDif, dea: point.macdDea, histogram: point.macdHist,
                                 k: point.kdjK, d: point.kdjD)
        }
    }

    static func visible(_ points: [NativeIndicatorPoint], context: VoiceChartContext?) -> [NativeIndicatorPoint] {
        guard let context, context.kind == "candles" else { return Array(points.suffix(60)) }
        guard context.visiblePointCount > 0, let first = context.firstVisibleTime,
              let last = context.lastVisibleTime else { return [] }
        return points.filter { $0.time >= first && $0.time <= last }
    }

    static func inspected(_ points: [NativeIndicatorPoint], context: VoiceChartContext?) -> NativeIndicatorPoint? {
        if let selected = context?.selectedPoint?.time, let point = points.first(where: { $0.time == selected }) {
            return point
        }
        return points.last
    }
}
