import Foundation

@main
enum StockTimelineChecks {
    static func main() {
        let morning = StockTimelineWindow(first: "09:30", last: "10:00", tradeDate: "20260918")!
        precondition(morning.contains("2026-09-18T09:45:00"))
        precondition(!morning.contains("2026-09-17T09:45:00"))
        precondition(!morning.contains("10:01", tradeDate: "2026-09-18"))
        // Daily candles still overlap a selected intraday interval for that date.
        precondition(morning.indices(in: ["2026-09-17", "2026-09-18"]) == 1..<2)

        let historical = StockTimelineWindow(first: "2026-08-03", last: "2026-08-07")!
        let minuteBars = ["2026-08-03 09:35", "2026-08-05 13:15", "2026-08-10 10:00"]
        precondition(historical.indices(in: minuteBars) == 0..<2)
        precondition(historical.indices(in: ["2026-09-18"]).isEmpty)
        precondition(historical.automaticPeriodID == "m15")

        let year = StockTimelineWindow(first: "2025-09-19", last: "2026-09-18")!
        precondition(year.automaticPeriodID == "day")
        // A shortened cache changes only available indices, not the requested window or precision.
        let before = year
        _ = year.indices(in: ["2026-09-17", "2026-09-18"])
        precondition(year == before && year.automaticPeriodID == "day")

        precondition(StockTimelineTime.sessionSlot("11:30") == 120)
        precondition(StockTimelineTime.sessionSlot("13:00") == 121)
        precondition(StockTimelineTime.sessionSlot("12:15") == nil)
        precondition(StockTimelineTime.sessionTime(241) == "15:00")
        print("Stock timeline date, cache-boundary and session checks passed")
    }
}
