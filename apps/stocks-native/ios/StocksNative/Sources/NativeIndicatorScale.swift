import Foundation

/// Deterministic domains shared by native indicator charts and screen-ink identity.
struct NativeIndicatorScale {
    let domain: ClosedRange<Double>

    static func macd(_ values: [Double]) -> Self {
        let values = values.filter(\.isFinite)
        let lower = min(0, values.min() ?? 0)
        let upper = max(0, values.max() ?? 0)
        guard lower < upper else { return Self(domain: -0.001...0.001) }
        let span = upper - lower
        let padding = span.isFinite ? span * 0.12 : max(abs(lower), abs(upper)) * 0.12
        let low = lower - padding
        let high = upper + padding
        return Self(domain: (low.isFinite ? low : lower)...(high.isFinite ? high : upper))
    }

    static func kdj(_ values: [Double]) -> Self {
        let values = values.filter(\.isFinite)
        return Self(domain: min(0, (values.min() ?? 0) - 5)...max(100, (values.max() ?? 100) + 5))
    }

    var tickStep: Double {
        let span = domain.upperBound - domain.lowerBound
        guard span.isFinite, span > 0 else { return 1 }
        let raw = span / 3
        let magnitude = pow(10, floor(log10(raw)))
        guard magnitude.isFinite, magnitude > 0 else { return span }
        let unit = raw / magnitude
        let step = [1.0, 2, 2.5, 5, 10].first(where: { $0 >= unit }) ?? 10
        return step * magnitude
    }

    var ticks: [Double] {
        let step = tickStep
        guard step.isFinite, step > 0 else { return [0] }
        let start = ceil(domain.lowerBound / step)
        let end = floor(domain.upperBound / step)
        guard start.isFinite, end.isFinite, end >= start, end - start <= 8 else { return [0] }
        return (0...Int(end - start)).map { offset in
            let value = (start + Double(offset)) * step
            return abs(value) < step * 1e-10 ? 0 : value
        }
    }

    private var decimalPlaces: Int {
        let step = tickStep
        guard step.isFinite, step > 0 else { return 2 }
        let power = max(0, min(16, Int(ceil(-log10(step)))))
        let scaled = step * pow(10, Double(power))
        return power + (abs(scaled - scaled.rounded()) > 1e-7 ? 1 : 0)
    }

    func label(_ value: Double) -> String {
        formatted(value, places: decimalPlaces)
    }

    func valueLabel(_ value: Double?) -> String {
        guard let value else { return "—" }
        return formatted(value, places: max(3, decimalPlaces + 1))
    }

    private func formatted(_ value: Double, places: Int) -> String {
        guard value.isFinite else { return "—" }
        if value == 0 { return "0" }
        if places > 8 || abs(value) >= 1e9 {
            return String(format: "%.2e", locale: Locale(identifier: "en_US_POSIX"), value)
        }
        let roundingUnit = pow(10, -Double(places))
        if abs(value) < roundingUnit * 0.5 { return "0" }
        return String(format: "%.*f", locale: Locale(identifier: "en_US_POSIX"), places, value)
    }
}
