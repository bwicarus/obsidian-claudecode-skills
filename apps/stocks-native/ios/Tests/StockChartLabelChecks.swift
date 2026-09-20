import Foundation

@main
struct StockChartLabelChecks {
    static func main() {
        precondition(StockChartLabels.detail("202609181330") == "2026/09/18 13:30")
        precondition(StockChartLabels.detail("2026-09-18T13:30:01") == "2026/09/18 13:30")
        precondition(StockChartLabels.detail("2026-09-18T05:30:00Z") == "2026/09/18 13:30")
        precondition(StockChartLabels.detail("13:30", tradeDate: "20260918") == "2026/09/18 13:30")
        precondition(StockChartLabels.day("20260918") == "2026/09/18")
        precondition(StockChartLabels.detail("202602301330") == "—")
        precondition(StockChartLabels.detail(nil) == "—")
        let minuteTicks = StockChartLabels.ticks(times: ["202609181330", "202609181335", "202609181340"], positions: [35, 36, 37])
        precondition(minuteTicks.map(\.label) == ["13:30", "13:35", "13:40"])
        precondition(minuteTicks.map(\.position) == [35, 36, 37])
        let days = StockChartLabels.ticks(times: ["20261230", "20261231", "20270104", "20270105"])
        precondition(days.contains { $0.label == "2027" })
        let acrossDays = StockChartLabels.ticks(times: ["202609181455", "202609181500", "202609210930", "202609210935"])
        precondition(acrossDays.first?.label == "09/18 14:55")
        precondition(acrossDays.contains { $0.label == "09/21 09:30" })
        precondition(StockChartLabels.ticks(times: ["invalid"]).isEmpty)
        precondition(StockChartLabels.ticks(times: ["20260918"], positions: []).isEmpty)
        print("Stock chart label checks passed: compact / ISO / market timezone / intraday / year boundary")
    }
}
