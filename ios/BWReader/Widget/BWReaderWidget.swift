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

@main
struct BWReaderWidgetBundle: WidgetBundle {
    var body: some Widget {
        BWReaderRecentWidget()
    }
}
