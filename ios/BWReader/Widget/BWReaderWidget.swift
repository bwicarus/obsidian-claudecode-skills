import Foundation
import SwiftUI
import WidgetKit

private struct ReaderSnapshotEntry: TimelineEntry {
    let date: Date
    let snapshot: ReaderSharedSnapshot?
}

private struct ReaderSnapshotProvider: TimelineProvider {
    private let store = ReaderNativeFeatureStore()

    func placeholder(in context: Context) -> ReaderSnapshotEntry {
        ReaderSnapshotEntry(
            date: Date(),
            snapshot: ReaderSharedSnapshot(
                title: "BWReader",
                url: "https://bwicarus.taile44d0c.ts.net/pdf/",
                page: "12",
                pageCount: "120",
                visibleText: "继续最近的阅读"
            )
        )
    }

    func getSnapshot(
        in context: Context,
        completion: @escaping (ReaderSnapshotEntry) -> Void
    ) {
        completion(
            ReaderSnapshotEntry(
                date: Date(),
                snapshot: context.isPreview ? placeholder(in: context).snapshot : store.readSnapshot()
            )
        )
    }

    func getTimeline(
        in context: Context,
        completion: @escaping (Timeline<ReaderSnapshotEntry>) -> Void
    ) {
        let now = Date()
        let entry = ReaderSnapshotEntry(date: now, snapshot: store.readSnapshot())
        completion(
            Timeline(
                entries: [entry],
                policy: .after(now.addingTimeInterval(15 * 60))
            )
        )
    }
}

private struct ReaderWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: ReaderSnapshotEntry

    private var title: String {
        guard let snapshot = entry.snapshot else { return "BWReader" }
        let trimmed = snapshot.title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "最近阅读" : trimmed
    }

    private var detail: String {
        guard let snapshot = entry.snapshot else {
            return "点击打开 BWReader"
        }
        let selection = snapshot.selection.trimmingCharacters(in: .whitespacesAndNewlines)
        if !selection.isEmpty {
            return selection
        }
        let visibleText = snapshot.visibleText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !visibleText.isEmpty {
            return visibleText
        }
        return "点击继续阅读"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "books.vertical.fill")
                    .foregroundStyle(.tint)
                Text("BWReader")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }

            Text(title)
                .font(.headline)
                .lineLimit(family == .systemSmall ? 3 : 2)

            if let snapshot = entry.snapshot {
                Text(snapshot.pageSummary)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.tint)
                    .lineLimit(1)

                if family == .systemMedium {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }

                Spacer(minLength: 0)

                HStack(spacing: 4) {
                    Image(systemName: "clock")
                    Text(snapshot.updatedAt, style: .relative)
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
            } else {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                Spacer(minLength: 0)
            }
        }
        .containerBackground(for: .widget) {
            LinearGradient(
                colors: [
                    Color(uiColor: .systemBackground),
                    Color.accentColor.opacity(0.10),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        }
        .widgetURL(URL(string: "bwreader://reader-feature?action=openReader"))
    }
}

struct BWReaderRecentWidget: Widget {
    let kind = "BWReaderRecentWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: ReaderSnapshotProvider()) { entry in
            ReaderWidgetView(entry: entry)
        }
        .configurationDisplayName("最近阅读")
        .description("快速打开 BWReader；共享进度可用时会自动显示。")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

// ── 系统投影三件套（2026-08-27 用户拍板：按功能分开）────────────────
// 数据来自 App Group 的 widget-system.json（App 系统投影同步时写入）。
// 快照模型，不假装实时 —— 每个组件都显示"数据时刻"。

private struct SystemDataEntry: TimelineEntry {
    let date: Date
    let data: ReaderWidgetSystemData?
}

private struct SystemDataProvider: TimelineProvider {
    private let store = ReaderNativeFeatureStore()

    func placeholder(in context: Context) -> SystemDataEntry {
        SystemDataEntry(
            date: Date(),
            data: ReaderWidgetSystemData(
                review: .init(due: 12, newCards: 3, atMs: 0),
                notifications: [
                    .init(id: "p", title: "今晚整理错题", kind: "todo",
                          state: "pending"),
                ],
                lastSyncAtMs: Int64(
                    Date().timeIntervalSince1970 * 1000) - 300_000,
                updatedAtMs: Int64(Date().timeIntervalSince1970 * 1000)))
    }

    func getSnapshot(
        in context: Context,
        completion: @escaping (SystemDataEntry) -> Void
    ) {
        completion(SystemDataEntry(
            date: Date(),
            data: context.isPreview
                ? placeholder(in: context).data
                : store.readWidgetSystemData()))
    }

    func getTimeline(
        in context: Context,
        completion: @escaping (Timeline<SystemDataEntry>) -> Void
    ) {
        let now = Date()
        completion(Timeline(
            entries: [SystemDataEntry(
                date: now, data: store.readWidgetSystemData())],
            policy: .after(now.addingTimeInterval(15 * 60))))
    }
}

private func dataAge(_ ms: Int64) -> String {
    guard ms > 0 else { return "" }
    let minutes = max(
        0,
        Int((Date().timeIntervalSince1970 * 1000
            - Double(ms)) / 60_000))
    return minutes < 60
        ? "\(minutes) 分钟前"
        : "\(minutes / 60) 小时前"
}

private struct ReviewWidgetView: View {
    let entry: SystemDataEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("复习", systemImage: "rectangle.stack")
                .font(.caption).foregroundStyle(.secondary)
            if let review = entry.data?.review {
                Text("\(review.due)")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                Text("到期待复习 · 新卡 \(review.newCards)")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Text(dataAge(review.atMs))
                    .font(.caption2).foregroundStyle(.tertiary)
            } else {
                Text("暂无数据")
                    .font(.caption).foregroundStyle(.secondary)
                Text("打开 App 同步一次即可")
                    .font(.caption2).foregroundStyle(.tertiary)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .containerBackground(for: .widget) {
            Color(uiColor: .systemBackground)
        }
        .widgetURL(URL(string: "bwreader://reader-feature?action=openReader"))
    }
}

private struct NotificationsWidgetView: View {
    let entry: SystemDataEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Label("待办通知", systemImage: "bell")
                .font(.caption).foregroundStyle(.secondary)
            let items = entry.data?.notifications ?? []
            if items.isEmpty {
                Text("没有待办通知")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            } else {
                ForEach(items.prefix(4), id: \.id) { item in
                    HStack(spacing: 5) {
                        Circle()
                            .fill(item.state == "pending"
                                ? Color.orange : Color.secondary.opacity(0.5))
                            .frame(width: 6, height: 6)
                        Text(item.title)
                            .font(.caption)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                Text(dataAge(entry.data?.updatedAtMs ?? 0))
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .containerBackground(for: .widget) {
            Color(uiColor: .systemBackground)
        }
        .widgetURL(URL(string: "bwreader://reader-feature?action=openReader"))
    }
}

private struct SyncWidgetView: View {
    let entry: SystemDataEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("同步", systemImage: "arrow.triangle.2.circlepath")
                .font(.caption).foregroundStyle(.secondary)
            if let data = entry.data, data.lastSyncAtMs > 0 {
                HStack(spacing: 5) {
                    Circle()
                        .fill(Date().timeIntervalSince1970 * 1000
                            - Double(data.lastSyncAtMs) < 45 * 60_000
                            ? Color.green : Color.orange)
                        .frame(width: 8, height: 8)
                    Text("已同步")
                        .font(.headline)
                }
                Text(dataAge(data.lastSyncAtMs))
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer(minLength: 0)
            } else {
                Text("尚未同步")
                    .font(.caption).foregroundStyle(.secondary)
                Text("打开 App 与电脑同步")
                    .font(.caption2).foregroundStyle(.tertiary)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .containerBackground(for: .widget) {
            Color(uiColor: .systemBackground)
        }
        .widgetURL(URL(string: "bwreader://reader-feature?action=openReader"))
    }
}

struct BWReaderReviewWidget: Widget {
    let kind = "BWReaderReviewWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SystemDataProvider()) {
            ReviewWidgetView(entry: $0)
        }
        .configurationDisplayName("复习")
        .description("今日到期与新卡数（随 App 同步更新）。")
        .supportedFamilies([.systemSmall])
    }
}

struct BWReaderNotificationsWidget: Widget {
    let kind = "BWReaderNotificationsWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SystemDataProvider()) {
            NotificationsWidgetView(entry: $0)
        }
        .configurationDisplayName("待办通知")
        .description("AI 待办与提醒（随 App 同步更新）。")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct BWReaderSyncWidget: Widget {
    let kind = "BWReaderSyncWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SystemDataProvider()) {
            SyncWidgetView(entry: $0)
        }
        .configurationDisplayName("同步状态")
        .description("最后一次与电脑成功同步的时间。")
        .supportedFamilies([.systemSmall])
    }
}

@main
struct BWReaderWidgetBundle: WidgetBundle {
    var body: some Widget {
        BWReaderRecentWidget()
        BWReaderReviewWidget()
        BWReaderNotificationsWidget()
        BWReaderSyncWidget()
    }
}
