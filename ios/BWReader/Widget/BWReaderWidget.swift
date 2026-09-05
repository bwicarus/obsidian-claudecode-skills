import AppIntents
import Foundation
import SwiftUI
import UIKit
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
    /// 展示板卡片图，按 sha 索引。WidgetKit 的视图里不能异步加载图片 ——
    /// 图必须在 timeline provider 里取好、随 entry 一起交出去。
    var cardImages: [String: UIImage] = [:]
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
                updatedAtMs: Int64(Date().timeIntervalSince1970 * 1000),
                boards: [
                    .init(code: "bd_preview", title: "发布看板",
                          updatedAtMs: Int64(
                              Date().timeIntervalSince1970 * 1000) - 600_000,
                          sections: [],
                          cards: [
                            .init(id: "p1", alt: "当前状态：尚未发布",
                                  sha: "placeholder-1", updatedAtMs: 0),
                            .init(id: "p2", alt: "已确认的信号",
                                  sha: "placeholder-2", updatedAtMs: 0),
                          ]),
                ],
                boardsError: nil))
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
            // 展示板卡片图：Windows 渲好、按 sha 内容寻址。有缓存直接用，
            // 没有才下载；下载失败那张卡显示 alt 文字，其它卡不受影响。
            // 跨板子铺格（2026-09-05 用户实拍：五块板子只显示了第一块），
            // 所以图也要按同一顺序跨板子取；形状与张数按这一档的版面定，
            // 多取的图只白占内存。
            let orderedCards = (data?.boards ?? []).flatMap { $0.cards ?? [] }
            let layout = BoardWidgetView.layout(
                family: context.family, count: orderedCards.count)
            let images = await WidgetCardImageCache.images(
                for: Array(orderedCards.prefix(layout.capacity)), shape: layout.shape)
            completion(Timeline(
                entries: [SystemDataEntry(
                    date: now, data: data, cardImages: images)],
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
            var components = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute],
                from: Date(timeIntervalSince1970: Double(at) / 1000))
            // 与 App 侧同一处理：不钉时区就是浮动墙钟时间。两份副本
            // 共用同一批 identifier，只改一处会让两边互相覆盖出不同的
            // 触发时刻。
            components.timeZone = Calendar.current.timeZone
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
        // 展示板（2026-09-05）。桥已经按"最近更新在前"排好序，这里不再排 ——
        // 两处各自排序就会在"同一时刻更新"的板子上给出不同的第一块。
        let boards = (root["boards"] as? [[String: Any]] ?? []).compactMap {
            one -> ReaderWidgetSystemData.Board? in
            guard let code = one["code"] as? String,
                  let title = one["title"] as? String else { return nil }
            let sections = (one["sections"] as? [[String: Any]] ?? [])
                .compactMap { raw -> ReaderWidgetSystemData.Board.Section? in
                    guard let sectionTitle = raw["title"] as? String
                    else { return nil }
                    return ReaderWidgetSystemData.Board.Section(
                        title: sectionTitle,
                        lines: (raw["lines"] as? [String] ?? []))
                }
            let cards = (one["cards"] as? [[String: Any]] ?? [])
                .compactMap { raw -> ReaderWidgetSystemData.Board.Card? in
                    guard let id = raw["id"] as? String,
                          let sha = raw["sha"] as? String else { return nil }
                    return ReaderWidgetSystemData.Board.Card(
                        id: id,
                        alt: raw["alt"] as? String ?? "",
                        sha: sha,
                        updatedAtMs:
                            (raw["updatedAtMs"] as? NSNumber)?.int64Value ?? 0)
                }
            return ReaderWidgetSystemData.Board(
                code: code,
                title: title,
                updatedAtMs:
                    (one["updatedAtMs"] as? NSNumber)?.int64Value ?? 0,
                sections: sections,
                cards: cards)
        }
        let now = Int64(Date().timeIntervalSince1970 * 1000)
        return ReaderWidgetSystemData(
            review: review,
            notifications: items,
            lastSyncAtMs: now,
            updatedAtMs: now,
            boards: boards,
            // ⚠ 桥说读不到就照实带上，**不要**在这里折成空数组：
            //   空板子会被下游当权威，于是"桥那边出问题"看起来像"AI 没写"。
            boardsError: root["boardsError"] as? String)
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

/// 展示板卡片图的本地缓存。**内容寻址**：文件名就是 sha，同名内容永不变，
/// 所以命中即用、永不过期；只按"还在板子上的 sha"清孤儿。
private enum WidgetCardImageCache {
    private static let maxCards = 12
    private static var directory: URL? {
        FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask).first?
            .appendingPathComponent("bw-board-cards", isDirectory: true)
    }

    static func images(
        for cards: [ReaderWidgetSystemData.Board.Card],
        shape: BoardWidgetView.CardShape
    ) async -> [String: UIImage] {
        guard let directory, !cards.isEmpty else { return [:] }
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true)
        let wanted = Array(cards.prefix(maxCards))
        var out: [String: UIImage] = [:]
        // 任务组里只传 Data（Sendable）；UIImage 在收口处再建 —— 免得撞严格并发检查。
        await withTaskGroup(of: (String, Data?).self) { group in
            for card in wanted {
                group.addTask {
                    (card.sha, await fetch(sha: card.sha, shape: shape, directory: directory))
                }
            }
            for await (sha, data) in group {
                if let data, let image = UIImage(data: data) { out[sha] = image }
            }
        }
        prune(keeping: Set(wanted.map(\.sha)), directory: directory)
        return out
    }

    /// 图按「sha.形状」存取：同一张卡 Windows 渲了方、宽两种，版面按张数挑一种
    /// （见 BoardWidgetView.layout）。
    private static func fetch(
        sha: String, shape: BoardWidgetView.CardShape, directory: URL
    ) async -> Data? {
        let file = directory.appendingPathComponent(sha + "." + shape.rawValue + ".png")
        if let data = try? Data(contentsOf: file), UIImage(data: data) != nil {
            return data
        }
        guard sha.count == 16, sha.allSatisfy(\.isHexDigit),
              let url = URL(string:
                "https://bwicarus-2.taile44d0c.ts.net/reader-board/card.png?sha="
                + sha + "&shape=" + shape.rawValue)
        else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              UIImage(data: data) != nil
        else { return nil }
        // 只在真解出图之后才落盘：半张/错误页缓存下来就是"这张卡永远是灰的"。
        try? data.write(to: file, options: [.atomic])
        return data
    }

    private static func prune(keeping: Set<String>, directory: URL) {
        guard let items = try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil) else { return }
        for item in items where item.pathExtension == "png" {
            // 文件名是 <sha>.<shape>.png；按 sha 判活、两种形状一起留 ——
            // 不同档的组件各要一种形状，互删对方的图只会反复重下。
            let sha = item.lastPathComponent.split(separator: ".").first.map(String.init) ?? ""
            if !keeping.contains(sha) {
                try? FileManager.default.removeItem(at: item)
            }
        }
    }
}

/// 每张卡固定的删除键（用户 2026-09-05：「固定每张卡片有一个删除按键」）。
/// iOS 17 小组件的 Button(intent:) —— 直接打桥的 cardDelete，再刷 timeline。
struct DeleteBoardCardIntent: AppIntent {
    static var title: LocalizedStringResource = "删除展示板卡片"
    static var isDiscoverable: Bool = false

    @Parameter(title: "板子编码") var code: String
    @Parameter(title: "卡片 ID") var cardId: String

    init() {}
    init(code: String, cardId: String) {
        self.code = code
        self.cardId = cardId
    }

    func perform() async throws -> some IntentResult {
        guard let url = URL(
            string: "https://bwicarus-2.taile44d0c.ts.net/reader-board/v1")
        else { return .result() }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "op": "cardDelete", "code": code, "id": cardId,
        ])
        _ = try? await URLSession.shared.data(for: request)
        // 不管成没成都刷一次：成了立刻消失；没成下一次拉取会把真相带回来。
        WidgetCenter.shared.reloadTimelines(ofKind: "BWReaderBoardWidget")
        return .result()
    }
}

/// 展示板（2026-09-05 用户改版）：一块**方格板**，每张卡一个方块，
/// 内容由电脑上的 AI 写成 HTML/CSS、在 Windows 渲成图，这里只显示。
///
/// 三条纪律：
/// - **板子不解释内容**。它不排版、不加工、不补时间戳 —— 写板子的人写什么就显示什么。
/// - **数据时刻要露出来**。小组件是快照，不假装实时；旧了必须让人看得出来，
///   否则会拿"拉取成功"冒充"内容新鲜"。
/// - **读不到 ≠ 空**。桥报了 boardsError 就说"暂时取不到"，绝不显示一块空板子。
private struct BoardWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: SystemDataEntry

    private var boards: [ReaderWidgetSystemData.Board] {
        entry.data?.boards ?? []
    }

    /// 一格一张卡，**跨板子**按顺序铺（2026-09-05 用户实拍：AI 建了五块板子，
    /// 特大号 8 格只显示了第一块的 1 张卡，其余四块折成一行「另有 4 块板子」）。
    /// 桥已按最近更新给板子排序、板内按写入顺序，所以格子先给最新的板子。
    /// 每格记着自己属于哪块板子：删除键要带板子编码，多板同屏时角标写板名。
    struct Slot: Identifiable {
        let board: ReaderWidgetSystemData.Board
        let card: ReaderWidgetSystemData.Board.Card
        var id: String { board.code + "/" + card.id }
    }

    /// 卡片形状。Windows 每张卡渲两种，这里只挑不裁；名字与桥的 `shape` 参数、
    /// `board_card_render.SHAPES` 三处一致。
    enum CardShape: String {
        case square   // 1:1，320×320 逻辑像素
        case wide     // 2:1，640×320
    }

    struct Layout {
        let columns: Int
        let rows: Int
        let shape: CardShape
        var capacity: Int { columns * rows }
        var aspectRatio: CGFloat { shape == .wide ? 2 : 1 }
    }

    /// 版面按「几张卡 × 哪一档」定（2026-09-05 用户实拍：特大号 4 张卡只占上半，
    /// 下半整行空着，字还小）。方卡在 2:1 的组件里放 4 张，无论怎么排都只能占
    /// 一半 —— 所以卡少时改用宽卡把面积吃满，卡多时才用方卡。
    static func layout(family: WidgetFamily, count: Int) -> Layout {
        switch family {
        case .systemSmall:
            return Layout(columns: 1, rows: 1, shape: .square)
        case .systemMedium:   // 2:1
            return count <= 1
                ? Layout(columns: 1, rows: 1, shape: .wide)
                : Layout(columns: 2, rows: 1, shape: .square)
        case .systemLarge:    // 1:1
            switch count {
            case ...1: return Layout(columns: 1, rows: 1, shape: .square)
            case 2: return Layout(columns: 1, rows: 2, shape: .wide)
            default: return Layout(columns: 2, rows: 2, shape: .square)
            }
        default:              // systemExtraLarge，2:1
            switch count {
            case ...1: return Layout(columns: 1, rows: 1, shape: .wide)
            case 2: return Layout(columns: 2, rows: 1, shape: .square)
            case 3...4: return Layout(columns: 2, rows: 2, shape: .wide)
            default: return Layout(columns: 4, rows: 2, shape: .square)   // 8 张
            }
        }
    }

    private var allSlots: [Slot] {
        boards.flatMap { board in (board.cards ?? []).map { Slot(board: board, card: $0) } }
    }

    private var layout: Layout {
        Self.layout(family: family, count: allSlots.count)
    }

    private var slots: [Slot] {
        Array(allSlots.prefix(layout.capacity))
    }

    /// 格子里出现了几块不同的板子。只有一块时标题就写它的名字；
    /// 多于一块时标题写总数，每张卡角上标它属于哪块。
    private var shownBoardCodes: Set<String> { Set(slots.map(\.board.code)) }

    private var headerTitle: String {
        if shownBoardCodes.count <= 1, let only = slots.first?.board ?? boards.first {
            return only.title
        }
        return "展示板 · \(boards.count) 块"
    }

    /// 数据时刻取所有板子里最新的那一次 —— 板子是快照，旧了必须让人看得出来。
    private var newestUpdatedAtMs: Int64 {
        boards.map(\.updatedAtMs).max() ?? 0
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !boards.isEmpty {
                HStack(spacing: 4) {
                    Image(systemName: "square.grid.2x2")
                        .font(.caption2)
                    Text(headerTitle)
                        .font(.caption).bold()
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    Text(dataAge(newestUpdatedAtMs))
                        .font(.caption2).foregroundStyle(.tertiary)
                }
                .foregroundStyle(.secondary)
                if slots.isEmpty {
                    Spacer(minLength: 0)
                    Text(boards.count == 1 ? "这块板子暂时是空的" : "板子暂时都是空的")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                } else {
                    cardGrid()
                }
                // 放不下的按**卡**报数，不按板子 —— 用户要知道的是漏看了几张。
                let hidden = allSlots.count - slots.count
                if hidden > 0, family != .systemSmall {
                    Text("还有 \(hidden) 张卡放不下")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            } else if let failure = entry.data?.boardsError {
                Label("展示板", systemImage: "square.grid.2x2")
                    .font(.caption).foregroundStyle(.secondary)
                Text("暂时取不到")
                    .font(.caption).foregroundStyle(.secondary)
                // 错误码照实显示：这块板子没有控制台，不写出来就等于静默。
                Text(failure)
                    .font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
                Spacer(minLength: 0)
            } else {
                Label("展示板", systemImage: "square.grid.2x2")
                    .font(.caption).foregroundStyle(.secondary)
                Text("还没有板子")
                    .font(.caption).foregroundStyle(.secondary)
                Text("在任务里说明要用展示板，AI 会申请一块")
                    .font(.caption2).foregroundStyle(.tertiary)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .containerBackground(for: .widget) {
            Color(uiColor: .systemBackground)
        }
        .widgetURL(URL(string: "bwreader://reader-feature?action=openReader"))
    }

    /// 方格本体。每格一张卡：渲好的图铺满方块；取不到图就显示 alt 文字。
    /// 右上角固定一个删除键（Button(intent:)），谁写的卡都能被用户一键撤掉。
    private func cardGrid() -> some View {
        let layout = self.layout
        let columns = Array(
            repeating: GridItem(.flexible(), spacing: 6), count: layout.columns)
        let labelBoards = shownBoardCodes.count > 1
        let ratio = layout.aspectRatio
        return LazyVGrid(columns: columns, spacing: 6) {
            ForEach(slots) { slot in
                let card = slot.card
                ZStack(alignment: .topTrailing) {
                    Group {
                        if let image = entry.cardImages[card.sha] {
                            Image(uiImage: image)
                                .resizable()
                                .aspectRatio(ratio, contentMode: .fill)
                        } else {
                            // 没图：alt 文字兜底（图还没渲出来 / 下载失败）。
                            // 不留空方块 —— 空方块会被当成"这张卡是空的"。
                            ZStack {
                                Color(uiColor: .secondarySystemBackground)
                                Text(card.alt.isEmpty ? "渲染中…" : card.alt)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .multilineTextAlignment(.center)
                                    .padding(6)
                            }
                        }
                    }
                    .aspectRatio(ratio, contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(alignment: .bottomLeading) {
                        // 多板同屏时才标板名：只有一块时标题已经说了。
                        if labelBoards {
                            Text(slot.board.title)
                                .font(.system(size: 9, weight: .semibold))
                                .lineLimit(1)
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6).padding(.vertical, 3)
                                .background(Color.black.opacity(0.55), in: Capsule())
                                .padding(5)
                        }
                    }
                    Button(intent: DeleteBoardCardIntent(code: slot.board.code, cardId: card.id)) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.white)
                            .padding(5)
                            .background(Color.black.opacity(0.55), in: Circle())
                    }
                    .buttonStyle(.plain)
                    .padding(4)
                }
            }
        }
    }
}

struct BWReaderBoardWidget: Widget {
    let kind = "BWReaderBoardWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: SystemDataProvider()) {
            BoardWidgetView(entry: $0)
        }
        .configurationDisplayName("展示板")
        .description("电脑上的任务往这块方格板里放卡片（每日新闻、发布盯梢、长任务进展）；每张卡都能一键删。")
        // 大号（iPad 专有）是用户点名要的：「首先应该支持更大的版本」。
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge])
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
        BWReaderBoardWidget()
    }
}
