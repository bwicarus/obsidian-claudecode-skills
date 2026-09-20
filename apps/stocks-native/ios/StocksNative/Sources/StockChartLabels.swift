import Foundation

struct StockChartTick {
    let position: Double
    let label: String
}

/// Exchange-local labels; axis density is independent of the source timestamp encoding.
enum StockChartLabels {
    private struct Stamp {
        let date: Date
        let key: String
        let hasTime: Bool
        var day: String { String(key.prefix(8)) }
        var month: String { String(key.prefix(6)) }
        var year: String { String(key.prefix(4)) }
    }

    private static func formatter(_ format: String) -> DateFormatter {
        let value = DateFormatter()
        value.locale = Locale(identifier: "en_US_POSIX")
        value.calendar = Calendar(identifier: .gregorian)
        value.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        value.dateFormat = format
        value.isLenient = false
        return value
    }

    private static func parse(_ time: String?, tradeDate: String? = nil, using parser: DateFormatter) -> Stamp? {
        guard let time, !time.isEmpty else { return nil }
        let raw = time.filter { $0.isASCII && $0.isNumber }
        let hasTime = raw.count >= 12 || (raw.count <= 6 && tradeDate != nil)
        // Offset-bearing ISO values represent an instant; compact/zone-less values are market clocks.
        if time.contains("T"), time.hasSuffix("Z") || time.range(of: "[+-][0-9]{2}:[0-9]{2}$", options: .regularExpression) != nil {
            let iso = ISO8601DateFormatter()
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var date = iso.date(from: time)
            if date == nil { iso.formatOptions = [.withInternetDateTime]; date = iso.date(from: time) }
            if let date { return Stamp(date: date, key: parser.string(from: date), hasTime: true) }
            return nil
        }
        guard let key = StockTimelineTime.key(time, tradeDate: tradeDate),
              let date = parser.date(from: key), parser.string(from: date) == key else { return nil }
        return Stamp(date: date, key: key, hasTime: hasTime)
    }

    static func detail(_ time: String?, tradeDate: String? = nil) -> String {
        guard let stamp = parse(time, tradeDate: tradeDate, using: formatter("yyyyMMddHHmmss")) else { return "—" }
        return formatter(stamp.hasTime ? "yyyy/MM/dd HH:mm" : "yyyy/MM/dd").string(from: stamp.date)
    }

    static func day(_ time: String?) -> String {
        guard let stamp = parse(time, using: formatter("yyyyMMddHHmmss")) else { return "—" }
        return formatter("yyyy/MM/dd").string(from: stamp.date)
    }

    static func ticks(times: [String], positions: [Double]? = nil, maxCount: Int = 4) -> [StockChartTick] {
        guard !times.isEmpty, maxCount > 0, positions == nil || positions?.count == times.count else { return [] }
        let parser = formatter("yyyyMMddHHmmss")
        let stamps = times.map { parse($0, using: parser) }
        let valid = stamps.indices.filter { stamps[$0] != nil && (positions?[$0] ?? Double($0)).isFinite }
        guard let first = valid.first, let last = valid.last, let start = stamps[first], let end = stamps[last] else { return [] }
        let count = min(maxCount, valid.count)
        let spanDays = abs(end.date.timeIntervalSince(start.date)) / 86400
        let intraday = start.hasTime || end.hasTime
        var selected = count == 1 ? [last] : [first, last]
        let spacing = count > 1 ? Double(last - first) / Double(count - 1) : 1
        // Prefer calendar boundaries near each slot, without bunching labels at an edge.
        if count > 2 {
            for slot in 1..<(count - 1) {
                let target = Double(first) + spacing * Double(slot)
                let candidates = valid.filter { index in
                    abs(Double(index) - target) <= spacing * 0.3 &&
                    selected.allSatisfy { abs(Double(index - $0)) >= spacing * 0.55 }
                }
                let best = candidates.max { lhs, rhs in
                    func score(_ index: Int) -> Double {
                        guard let now = stamps[index] else { return -.infinity }
                        let before = index > first ? stamps[index - 1] : nil
                        var weight = 0.0
                        if let before {
                            if before.year != now.year { weight = 4 }
                            else if before.month != now.month, spanDays > 20 { weight = 3 }
                            else if before.day != now.day, intraday { weight = 2 }
                        }
                        return weight - abs(Double(index) - target) / max(1, spacing)
                    }
                    return score(lhs) < score(rhs)
                }
                if let best { selected.append(best) }
            }
        }
        let clockFormat = formatter("HH:mm")
        let dateFormat = formatter("MM/dd")
        let monthFormat = formatter(start.year == end.year ? "M月" : "yyyy/MM")
        let crossDayFormat = formatter("MM/dd HH:mm")
        var previous: Stamp?
        return selected.sorted().compactMap { index in
            guard let stamp = stamps[index] else { return nil }
            let label: String
            if intraday, start.day == end.day {
                label = clockFormat.string(from: stamp.date)
            } else if intraday, spanDays <= 10 {
                label = previous?.day == stamp.day ? clockFormat.string(from: stamp.date) : crossDayFormat.string(from: stamp.date)
            } else if spanDays > 90 {
                label = monthFormat.string(from: stamp.date)
            } else if let previous, previous.year != stamp.year {
                label = stamp.year
            } else {
                label = dateFormat.string(from: stamp.date)
            }
            previous = stamp
            return StockChartTick(position: positions?[index] ?? Double(index), label: label)
        }
    }
}
