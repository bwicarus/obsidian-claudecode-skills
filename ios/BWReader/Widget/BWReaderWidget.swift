import Foundation
import SwiftUI
import UserNotifications
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

/// Widget 自己的缓存（2026-08-26 调研实锤：Widget target 在 project.yml
/// 里**没有** CODE_SIGN_ENTITLEMENTS —— App 与 Safari 扩展都有，唯独它
/// 没有。于是 `containerURL(forSecurityApplicationGroupIdentifier:)` 在
/// widget 进程里返回 nil，App Group 的读写一直静默无效（这也解释了
/// 「最近阅读」小组件为何长年显示占位内容）。
///
/// 修法选了零签名风险的一条：widget 既然已经自己拉数据，就**不需要**跟
/// App 共享容器 —— 缓存写进自己的沙箱即可。真要修 App Group 得在 Apple
/// 后台给 widget 的 App ID 开 App Groups 能力并重签描述文件，那是另一件
/// 事（会动到目前全绿的签名链），留作独立改动。
private enum WidgetLocalCache {
    private static var url: URL? {
        FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask).first?
            .appendingPathComponent("bw-widget-system.json")
    }

    static func read() -> ReaderWidgetSystemData? {
        guard let url, let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(
            ReaderWidgetSystemData.self, from: data)
    }

    static func write(_ value: ReaderWidgetSystemData) {
        guard let url, let data = try? JSONEncoder().encode(value) else {
            return
        }
        try? data.write(to: url, options: [.atomic])
    }
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
                : (WidgetLocalCache.read() ?? store.readWidgetSystemData())))
    }

    func getTimeline(
        in context: Context,
        completion: @escaping (Timeline<SystemDataEntry>) -> Void
    ) {
        // 小组件自己拉数据（2026-08-26 用户拍板：更新不能依赖 App 开启，
        // 也不该绑定某本书）。timeline 每 15 分钟刷新时直接经 Tailscale
        // 拉 Windows 桥的只读端点；成功写回 App Group 缓存（App 那条
        // 投影链仍在，两边共用同一份缓存），失败用缓存兜底 —— 缓存自带
        // 数据时刻，旧了用户看得出来。
        Task {
            let now = Date()
            var data = await Self.fetchRemote()
            if let fresh = data {
                WidgetLocalCache.write(fresh)
                await Self.scheduleDueNotifications(fresh)
                // App Group 目前在 widget 侧无效（见 WidgetLocalCache 注释），
                // 这行是"修好之后自动受益"的顺水人情，不承担正确性。
                try? store.writeWidgetSystemData(fresh)
            } else {
                data = WidgetLocalCache.read() ?? store.readWidgetSystemData()
            }
            completion(Timeline(
                entries: [SystemDataEntry(date: now, data: data)],
                policy: .after(now.addingTimeInterval(15 * 60))))
        }
    }

    /// widget 侧的到点通知排程（2026-08-26）。
    ///
    /// 为什么放在 widget 里：AI 在电脑上建的提醒，如果 iPad 到点前**没
    /// 打开过 App**，就没有任何人去排那条本地通知 —— 提醒到点不会响。
    /// widget 的 timeline 每 15–60 分钟被系统唤起一次（App 关着也跑），
    /// 顺路把到点通知排上，这个洞就补住了。
    ///
    /// 依据（Apple 官方，2026-08-26 实取）：UNUserNotificationCenter 的
    /// 文档原文是 "for your app **or app extension**"，UNError
    /// .notificationsNotAllowed 也明写 "your app or app extension"；
    /// App Extension Programming Guide 的禁止清单里没有通知。
    ///
    /// 纪律：
    /// - **绝不在 widget 里请求权限**（没有可交互的宿主），只在已授权时排；
    /// - identifier 与 App 侧**故意相同** —— 同 id 是替换语义，两边算的是
    ///   同一份服务器数据，谁先排都一样，不会重复打扰；
    /// - 排程结果随下次拉取上报给桥（见 fetchRemote 的 query），否则
    ///   widget 里的失败在真机上等价于静默。
    private static func scheduleDueNotifications(
        _ data: ReaderWidgetSystemData
    ) async {
        let center = UNUserNotificationCenter.current()
        guard await center.notificationSettings().authorizationStatus
            == .authorized else {
            lastScheduleOutcome = "denied"
            return
        }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        let due = data.notifications
            .compactMap { item -> (ReaderWidgetSystemData.NotificationItem, Int64)? in
                guard let at = item.dueAtMs, at > nowMs else { return nil }
                return (item, at)
            }
            .sorted { $0.1 < $1.1 }
            .prefix(32)
        // 撤销也要做，不能只加不删：用户已完成/取消的提醒若只由 App 侧
        // 清理，而用户恰恰长期不开 App（这正是 widget 排程存在的前提），
        // 那条通知到点还会响一次 —— 比不响更糟。
        // 只在**本轮真的拿到了新数据**时清理（调用点保证了这一点），
        // 拉取失败时绝不动已排好的通知。
        let wanted = Set(due.map { "bw-due-" + $0.0.id })
        let stale = (await center.pendingNotificationRequests())
            .map(\.identifier)
            .filter { $0.hasPrefix("bw-due-") && !wanted.contains($0) }
        if !stale.isEmpty {
            center.removePendingNotificationRequests(withIdentifiers: stale)
        }
        for (item, at) in due {
            let content = UNMutableNotificationContent()
            content.title = item.title
            if let body = item.body, !body.isEmpty { content.body = body }
            content.sound = .default
            content.interruptionLevel = .timeSensitive
            let components = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute],
                from: Date(timeIntervalSince1970: Double(at) / 1000))
            try? await center.add(UNNotificationRequest(
                identifier: "bw-due-" + item.id,
                content: content,
                trigger: UNCalendarNotificationTrigger(
                    dateMatching: components, repeats: false)))
        }
        lastScheduleOutcome = "scheduled=" + String(due.count)
    }

    /// 上一轮排程结果，随下次拉取捎给桥做诊断（widget 里没有控制台）。
    private static var lastScheduleOutcome = "init"

    private static func fetchRemote() async -> ReaderWidgetSystemData? {
        let reported = lastScheduleOutcome
            .addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) ?? "unknown"
        guard let url = URL(
            string: "https://bwicarus-2.taile44d0c.ts.net"
                + "/widget/system-data?widgetSchedule=" + reported)
        else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        guard let (payload, response) = try? await URLSession.shared
            .data(for: request),
            (response as? HTTPURLResponse)?.statusCode == 200,
            let root = try? JSONSerialization.jsonObject(with: payload)
                as? [String: Any],
            root["contract"] as? String == "reader-notifications/1"
        else { return nil }
        let items = (root["items"] as? [[String: Any]] ?? []).compactMap {
            one -> ReaderWidgetSystemData.NotificationItem? in
            guard let id = one["id"] as? String,
                  let title = one["title"] as? String else { return nil }
            let body = one["body"] as? String
            return ReaderWidgetSystemData.NotificationItem(
                id: id,
                title: title,
                kind: one["kind"] as? String ?? "",
                state: one["state"] as? String ?? "pending",
                body: (body?.isEmpty ?? true) ? nil : body,
                dueAtMs: (one["dueAtUtcMs"] as? NSNumber)?.int64Value)
        }
        var review: ReaderWidgetSystemData.Review?
        if let raw = root["review"] as? [String: Any],
           let due = (raw["due"] as? NSNumber)?.intValue,
           let fresh = (raw["new"] as? NSNumber)?.intValue {
            review = ReaderWidgetSystemData.Review(
                due: due,
                newCards: fresh,
                atMs: (raw["atMs"] as? NSNumber)?.int64Value ?? 0)
        }
        let now = Int64(Date().timeIntervalSince1970 * 1000)
        return ReaderWidgetSystemData(
            review: review,
            notifications: items,
            lastSyncAtMs: now,
            updatedAtMs: now)
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
