import Foundation

@main
enum NativeIndicatorScaleChecks {
    static func main() {
        let small = NativeIndicatorScale.macd([-0.005, 0.004, 0.001])
        precondition(small.domain.lowerBound < -0.005 && small.domain.upperBound > 0.004)
        precondition(small.domain.upperBound - small.domain.lowerBound < 0.02)
        precondition(small.ticks.contains(0) && small.ticks.count <= 5)
        precondition(small.label(0.005) == "0.005")
        precondition(small.label(-0.0) == "0" && small.label(-1e-12) == "0")

        let zero = NativeIndicatorScale.macd([0, 0, 0])
        precondition(zero.domain.lowerBound < 0 && zero.domain.upperBound > 0)
        precondition(zero.ticks.contains(0) && zero.label(0) == "0")
        let invalid = NativeIndicatorScale.macd([.nan, .infinity, -.infinity])
        precondition(invalid.domain == zero.domain)
        precondition(invalid.label(.nan) == "—")
        let filtered = NativeIndicatorScale.macd([.nan, -0.005, .infinity, 0.004, 0.001])
        precondition(filtered.domain == small.domain)

        let tiny = NativeIndicatorScale.macd([-5e-12, 4e-12])
        precondition(tiny.domain.upperBound - tiny.domain.lowerBound < 2e-11)
        let tinyLabel = tiny.label(5e-12)
        precondition(tinyLabel.lowercased().contains("e") && Double(tinyLabel) == 5e-12,
                     "Tiny indicator value lost precision: \(tinyLabel)")
        precondition(tiny.ticks.allSatisfy(\.isFinite))
        let positive = NativeIndicatorScale.macd([0.001, 0.005])
        precondition(positive.domain.lowerBound < 0 && positive.domain.upperBound > 0.005)
        let negative = NativeIndicatorScale.macd([-0.005, -0.001])
        precondition(negative.domain.lowerBound < -0.005 && negative.domain.upperBound > 0)
        let kdj = NativeIndicatorScale.kdj([-12, 50, 115, .nan])
        precondition(kdj.domain == -17...120)
        precondition(kdj.label(0) == "0")
        print("Native indicator scale: small, zero, nonfinite, tiny, one-sided and KDJ checks passed")
    }
}
