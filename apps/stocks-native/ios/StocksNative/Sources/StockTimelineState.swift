import Foundation

/// Absolute trading-time bounds survive a change of candle precision or cache size.
struct StockTimelineWindow: Equatable, Hashable {
    let lower: String
    let upper: String

    init?(first: String, last: String, tradeDate: String? = nil) {
        guard let lower = StockTimelineTime.key(first, tradeDate: tradeDate),
              let upper = StockTimelineTime.key(last, tradeDate: tradeDate, endOfDay: true),
              lower <= upper else { return nil }
        self.lower = lower; self.upper = upper
    }

    func contains(_ time: String, tradeDate: String? = nil) -> Bool {
        guard let first = StockTimelineTime.key(time, tradeDate: tradeDate),
              let last = StockTimelineTime.key(time, tradeDate: tradeDate, endOfDay: true) else { return false }
        return first <= upper && last >= lower
    }

    func indices(in times: [String], tradeDate: String? = nil) -> Range<Int> {
        let matches = times.indices.filter { contains(times[$0], tradeDate: tradeDate) }
        guard let first = matches.first, let last = matches.last else { return 0..<0 }
        return first..<(last + 1)
    }

    var calendarDays: Double {
        guard let start = StockTimelineTime.date(lower), let end = StockTimelineTime.date(upper) else { return 1 }
        return max(1, (end.timeIntervalSince(start) + 1) / 86400)
    }

    var automaticPeriodID: String {
        switch calendarDays {
        case ...1.01: return "intraday"
        case ...10: return "m15"
        case ...45: return "m60"
        case ...550: return "day"
        case ...1461: return "week"
        default: return "month"
        }
    }
}

enum StockTimelineTime {
    static func key(_ time: String, tradeDate: String? = nil, endOfDay: Bool = false) -> String? {
        let raw = time.filter { $0.isASCII && $0.isNumber }
        let digits: String
        if raw.count <= 6, let tradeDate {
            let day = String(tradeDate.filter { $0.isASCII && $0.isNumber }.prefix(8))
            guard day.count == 8 else { return nil }
            digits = day + raw
        } else { digits = raw }
        guard digits.count >= 8 else { return nil }
        if digits.count == 8 { return digits + (endOfDay ? "235959" : "000000") }
        guard digits.count >= 12 else { return nil }
        return String((digits + "000000").prefix(14))
    }

    static func date(_ key: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        formatter.dateFormat = "yyyyMMddHHmmss"
        return formatter.date(from: key)
    }

    static func day(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }

    static func timestamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return formatter.string(from: date)
    }

    static func label(_ key: String, includesTime: Bool) -> String {
        guard let date = date(key) else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = TimeZone(secondsFromGMT: 8 * 3600)
        formatter.dateFormat = includesTime ? "MM/dd HH:mm" : "yyyy/MM/dd"
        return formatter.string(from: date)
    }

    static func sessionTime(_ slot: Int) -> String {
        let slot = min(241, max(0, slot))
        let clock = slot <= 120 ? 570 + slot : 780 + slot - 121
        return String(format: "%02d:%02d", clock / 60, clock % 60)
    }

    static func sessionSlot(_ time: String) -> Int? {
        let parts = time.split(separator: ":")
        guard parts.count == 2, let hour = Int(parts[0]), let minute = Int(parts[1]) else { return nil }
        let clock = hour * 60 + minute
        if (570...690).contains(clock) { return clock - 570 }
        if (780...900).contains(clock) { return clock - 780 + 121 }
        return nil
    }
}
