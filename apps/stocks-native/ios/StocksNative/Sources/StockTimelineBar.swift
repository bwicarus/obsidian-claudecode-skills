import SwiftUI

struct StockTimelineBar: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Menu {
                    ForEach([1, 5, 30, 90, 365], id: \.self) { days in
                        Button(spanTitle(days)) { model.selectTimelineSpan(days: days) }
                    }
                } label: {
                    Label("范围", systemImage: "calendar")
                }
                Menu {
                    Button { model.selectTimelinePrecision(nil) } label: {
                        if model.timelinePrecisionOverride == nil { Label("自动精度", systemImage: "checkmark") }
                        else { Text("自动精度") }
                    }
                    ForEach(ChartPeriod.allCases) { period in
                        Button { model.selectTimelinePrecision(period) } label: {
                            if model.timelinePrecisionOverride == period { Label(period.title, systemImage: "checkmark") }
                            else { Text(period.title) }
                        }
                    }
                } label: {
                    HStack(spacing: 3) {
                        Text((model.timelinePrecisionOverride == nil ? "自动 · " : "") + model.chartPeriod.title)
                        Image(systemName: "chevron.down").font(.caption2)
                    }
                }
                Spacer(minLength: 0)
                Text(model.timelineRangeLabel).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                Button { model.moveTimelineToLatest() } label: { Image(systemName: "arrow.right.to.line") }
                    .accessibilityLabel("移到最新行情，保留时间跨度")
            }
            .font(.caption.weight(.medium)).buttonStyle(.plain).foregroundStyle(AppStyle.accent)
            .frame(minHeight: 28)
            ChartRangeNavigator(values: model.timelineValues,
                                selection: Binding(get: { model.timelineIndexRange }, set: { model.previewTimelineRange($0) }),
                                minimumCount: 2, compact: true,
                                onEditingChanged: model.timelineEditingChanged)
            if let coverage = model.timelineCoverageNote {
                Text(coverage).font(.caption2).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).lineLimit(2)
            }
        }
        .padding(.horizontal, 14).padding(.top, 2).padding(.bottom, 4)
        .background(.white)
    }

    private func spanTitle(_ days: Int) -> String {
        [1: "当日", 5: "近 5 个交易日", 30: "近 1 个月", 90: "近 3 个月", 365: "近 1 年"][days] ?? "近 \(days) 日"
    }
}
