import CoreLocation
import EventKit
import Foundation
import UserNotifications
import WidgetKit

/// iOS 系统投影（2026-08-27 用户拍板）：把 Windows 通知系统（唯一真值）
/// 投到三个系统表面 —— 苹果提醒事项（显示副本）、本地横幅通知（新
/// pending）、小组件共享数据（复习/待办/同步状态）。
///
/// 设计铁律：
/// - **单向投影 + 勾选回流**。Windows 是真值；苹果提醒里被用户勾完成的
///   条目以 resolvedIds 返回给 JS 走既有 resolve 回流（勾选 = 用户
///   resolve）。Windows 侧消失的条目在这里**删除**提醒 —— 显示副本
///   退场就该消失，留一堆已勾条目是噪音。
/// - 专用列表「BW 待办」，绝不碰用户自己的提醒列表。
/// - 权限被拒不是错误：提醒投影静默停用（本地通知与小组件照常），
///   但结果里如实报告 remindersState，绝不静默假装成功。
final class ReaderSystemProjection {
    static let shared = ReaderSystemProjection()

    private let eventStore = EKEventStore()
    private let store = ReaderNativeFeatureStore()
    private let defaults = UserDefaults(
        suiteName: ReaderNativeBridgeContract.appGroupIdentifier)
    private let mapKey = "bwReminderProjectionMapV1"
    private let seenKey = "bwNotificationSeenIdsV1"
    private let calendarKey = "bwReminderCalendarIdV1"
    private static let calendarTitle = "BW 待办"
    private static let maximumItems = 20
    private static let duePrefix = "bw-due-"
    // iOS 单 App 未触发的本地通知硬上限是 64（超出静默丢弃），留足余量。
    private static let maximumScheduledDue = 32

    struct Item {
        let id: String
        let title: String
        let kind: String
        let state: String
        let body: String
        // 到期时刻（行程/赶车类）：投影成苹果提醒的 dueDate + 闹钟，
        // 到点响铃由苹果系统负责 —— 比任何轮询都可靠。
        let dueAtMs: Int64?
        // 地点触发（「到家时提醒我倒垃圾」）：投影成苹果提醒的地理围栏
        // 闹钟。围栏由**系统提醒 App** 监控与触发，我们只要提醒事项
        // 权限 —— 不需要定位权限，也不占我们自己的 region 配额。
        let place: Place?
    }

    struct Place {
        let name: String
        let latitude: Double
        let longitude: Double
        let radiusMeters: Double
        /// "enter"（到达时）/ "leave"（离开时）
        let proximity: String
    }

    struct Outcome {
        let resolvedIds: [String]
        let remindersState: String
        let alarmsState: String
        // 通知权限被拒时整条横幅+到点通道都是哑的,而回执原来完全看不出
        // 来 —— 与本文件自己写的纪律相悖(见 silent-failure-lessons)。
        let notificationsState: String
    }

    // MARK: - 入口（bridge 的 system-projection action 调用）

    func apply(
        notifications: [Item],
        reviewDue: Int?,
        reviewNew: Int?,
        reviewAtMs: Int64?,
        syncAtMs: Int64
    ) async -> Outcome {
        writeWidgetData(
            notifications: notifications,
            reviewDue: reviewDue,
            reviewNew: reviewNew,
            reviewAtMs: reviewAtMs,
            syncAtMs: syncAtMs)
        let notificationsState =
            await postLocalNotificationsForNewPending(notifications)
        // ⚠ 顺序有讲究：**先**对账苹果提醒事项，拿到用户刚勾掉的
        // resolvedIds，再用"去掉已完成的"那批去排程。反过来的话，用户
        // 在提醒 App 里勾完成、这一轮却又把它的通知和闹钟重排一遍，
        // 到点还会响一次 —— 对已经做完的事，那一响比不响更糟。
        let (resolved, state) = await projectReminders(notifications)
        let armable = notifications.filter { !resolved.contains($0.id) }
        // 到点触发（用户诉求「提醒要真的叫醒我」第一层）：带 dueAt 的
        // 条目在到点时刻**排一条本地通知** —— 一旦排上，App 关掉、桥
        // 离线、iPad 断网都照响，这是唯一不依赖任何在线环节的通道。
        let dueState = await scheduleDueNotifications(armable)
        let alarms = await ReaderSystemAlarms.shared.sync(armable)
        return Outcome(
            resolvedIds: resolved,
            remindersState: state,
            alarmsState: alarms,
            notificationsState: notificationsState + "/" + dueState)
    }

    // MARK: - 小组件数据

    private func writeWidgetData(
        notifications: [Item],
        reviewDue: Int?,
        reviewNew: Int?,
        reviewAtMs: Int64?,
        syncAtMs: Int64
    ) {
        var review: ReaderWidgetSystemData.Review?
        if let due = reviewDue, let fresh = reviewNew {
            review = ReaderWidgetSystemData.Review(
                due: due, newCards: fresh, atMs: reviewAtMs ?? 0)
        }
        let value = ReaderWidgetSystemData(
            review: review,
            notifications: notifications.prefix(Self.maximumItems).map {
                ReaderWidgetSystemData.NotificationItem(
                    id: $0.id, title: $0.title,
                    kind: $0.kind, state: $0.state,
                    body: $0.body.isEmpty ? nil : $0.body,
                    dueAtMs: $0.dueAtMs)
            },
            lastSyncAtMs: syncAtMs,
            updatedAtMs: Int64(Date().timeIntervalSince1970 * 1000))
        do {
            try store.writeWidgetSystemData(value)
            WidgetCenter.shared.reloadAllTimelines()
        } catch {
            // 写不进共享容器时小组件保持旧数据（自带数据时刻，
            // 用户能看出它旧了）；不因此让整次投影失败。
        }
    }

    // MARK: - 本地横幅（新 pending 才响，去重持久化防重复打扰）

    private func postLocalNotificationsForNewPending(
        _ items: [Item]
    ) async -> String {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            _ = try? await center.requestAuthorization(
                options: [.alert, .badge, .sound])
        }
        guard await center.notificationSettings().authorizationStatus
            == .authorized else { return "denied" }
        var seen = Set(defaults?.stringArray(forKey: seenKey) ?? [])
        for item in items where item.state == "pending"
            && !seen.contains(item.id) {
            let content = UNMutableNotificationContent()
            content.title = item.title
            if !item.body.isEmpty { content.body = item.body }
            content.sound = .default
            // 时效级别：有 Time Sensitive entitlement 时可穿透专注模式；
            // 没有该 entitlement 时系统静默降级成普通级别（不会崩、不会
            // 拒发），所以这里无条件设 —— 想真正穿透专注模式需要在
            // 开发者后台给 App ID 开 capability 并重签描述文件。
            content.interruptionLevel = .timeSensitive
            try? await center.add(UNNotificationRequest(
                identifier: "bw-ntf-" + item.id,
                content: content,
                trigger: nil))
            seen.insert(item.id)
        }
        // 有界：只记还活着的 + 最近响过的，防止无限增长。
        let live = Set(items.map(\.id))
        defaults?.set(
            Array(seen.filter { live.contains($0) }.prefix(200)),
            forKey: seenKey)
        return "authorized"
    }

    // MARK: - 到点触发（本地通知排程）

    /// 带 dueAt 的条目排成到点本地通知。
    ///
    /// 为什么这条通道最重要：本地通知**一旦排上就归系统管**，App 被杀、
    /// 桥离线、iPad 断网都照响；而横幅只在同步那一刻响一次、苹果提醒
    /// 依赖提醒事项权限。三条并行，互为兜底。
    ///
    /// 幂等：每轮按当前条目全量重排（同 identifier 的 add 是替换语义），
    /// 已消失/已过期的条目撤销排程。iOS 对单 App 未触发的本地通知有 64
    /// 条硬上限（超了静默丢弃最远的），这里只排最近的 32 条留足余量。
    private func scheduleDueNotifications(_ items: [Item]) async -> String {
        let center = UNUserNotificationCenter.current()
        guard await center.notificationSettings().authorizationStatus
            == .authorized else { return "due=denied" }
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        // 已过点却还没被 resolve 的条目：不再静默丢弃。不补响（几小时后
        // 才响的赶车提醒没有意义、还会打扰），但要计数带进回执 —— 否则
        // "它到底响没响过"在设备上无从查起。
        var late = 0
        let due = items
            .compactMap { item -> (Item, Int64)? in
                guard let at = item.dueAtMs else { return nil }
                if at <= nowMs { late += 1; return nil }
                return (item, at)
            }
            .sorted { $0.1 < $1.1 }
            .prefix(Self.maximumScheduledDue)
        let wanted = Set(due.map { Self.duePrefix + $0.0.id })
        let stale = (await center.pendingNotificationRequests())
            .map(\.identifier)
            .filter { $0.hasPrefix(Self.duePrefix) && !wanted.contains($0) }
        if !stale.isEmpty {
            center.removePendingNotificationRequests(withIdentifiers: stale)
        }
        var failed = 0
        for (item, at) in due {
            let content = UNMutableNotificationContent()
            content.title = item.title
            if !item.body.isEmpty { content.body = item.body }
            content.sound = .default
            content.interruptionLevel = .timeSensitive
            let fire = Date(timeIntervalSince1970: Double(at) / 1000)
            var components = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute],
                from: fire)
            // ⚠ 必须钉住时区。不带 timeZone 的 components 是**浮动墙钟
            // 时间**：iOS 按"到时候设备在哪个时区"解析，出趟远门就会
            // 响在另一个绝对时刻，而 AlarmKit 与 EKAlarm 用的都是绝对
            // 时刻 —— 三条通道会各响各的。
            components.timeZone = Calendar.current.timeZone
            do {
                try await center.add(UNNotificationRequest(
                    identifier: Self.duePrefix + item.id,
                    content: content,
                    trigger: UNCalendarNotificationTrigger(
                        dateMatching: components, repeats: false)))
            } catch {
                // 排不上必须留痕：用户以为到点会响，实际这条根本没进
                // 系统。原来这里是 try? —— 失败与成功长得一模一样。
                failed += 1
            }
        }
        // 用系统**实际持有的**排程对账，而不是相信自己刚才的调用。
        let armed = (await center.pendingNotificationRequests())
            .filter { $0.identifier.hasPrefix(Self.duePrefix) }
            .count
        var state = "due=" + String(armed) + "/" + String(due.count)
        if failed > 0 { state += ";failed=" + String(failed) }
        if late > 0 { state += ";late=" + String(late) }
        return state
    }

    // MARK: - 提醒事项显示副本

    private func projectReminders(
        _ items: [Item]
    ) async -> ([String], String) {
        let status = EKEventStore.authorizationStatus(for: .reminder)
        if status == .notDetermined {
            let granted = (try? await eventStore
                .requestFullAccessToReminders()) ?? false
            if !granted { return ([], "denied") }
        } else if status != .fullAccess {
            return ([], "denied")
        }
        guard let calendar = ensureCalendar() else {
            return ([], "calendar-unavailable")
        }
        var map = (defaults?.dictionary(forKey: mapKey) as? [String: String])
            ?? [:]
        var failures = 0
        let liveIds = Set(items.map(\.id))
        var resolved: [String] = []

        // 勾选回流 + 真值退场清理。
        for (ntfId, reminderId) in map {
            guard let reminder = eventStore.calendarItem(
                withIdentifier: reminderId) as? EKReminder else {
                map.removeValue(forKey: ntfId)
                continue
            }
            if !liveIds.contains(ntfId) {
                // Windows 侧已 resolve/cancel/expire → 显示副本退场。
                try? eventStore.remove(reminder, commit: false)
                map.removeValue(forKey: ntfId)
            } else if reminder.isCompleted {
                resolved.append(ntfId)
            }
        }
        // upsert（用户已勾完成的不重建 —— 等回流让真值先退场）。
        for item in items.prefix(Self.maximumItems)
            where !resolved.contains(item.id) {
            let reminder: EKReminder
            if let existingId = map[item.id],
               let existing = eventStore.calendarItem(
                   withIdentifier: existingId) as? EKReminder {
                reminder = existing
            } else {
                reminder = EKReminder(eventStore: eventStore)
                reminder.calendar = calendar
            }
            reminder.title = item.title
            reminder.notes = item.body.isEmpty ? nil : item.body
            // 优先级：赶车/行程这类错过就没意义的排高，其余中等。
            // ⚠ reminder.priority 是 Int 而枚举 rawValue 是 UInt，必须显式
            // 转换；且只有 0-9 合法，其它值保存会失败（EKError
            // .priorityIsInvalid）。
            reminder.priority = Int(
                (item.kind == "trip" || item.dueAtMs != nil)
                    ? EKReminderPriority.high.rawValue
                    : EKReminderPriority.medium.rawValue)
            // 只保留由我们管理的闹钟，重复投影不叠加。
            reminder.alarms?.forEach(reminder.removeAlarm)
            if let dueMs = item.dueAtMs, dueMs > 0 {
                let due = Date(
                    timeIntervalSince1970: Double(dueMs) / 1000)
                var components = Calendar.current.dateComponents(
                    [.year, .month, .day, .hour, .minute], from: due)
                components.timeZone = Calendar.current.timeZone
                // ⚠⚠ iOS 上设了 due 就**必须**同时设 start，否则 save 抛
                // EKError.noStartDate —— 而下面的 catch 原来是 continue，
                // 于是"带到点时刻的提醒"可能从来就没进过苹果提醒，回执
                // 却一路全绿。这一条是 Apple 文档里 iOS 独有的约束
                // （macOS 没有），最容易在别处抄代码时漏掉。
                reminder.startDateComponents = components
                reminder.dueDateComponents = components
                reminder.addAlarm(EKAlarm(absoluteDate: due))
            }
            if let place = item.place {
                // 地理围栏闹钟：到达/离开某个已命名地点时由系统提醒 App
                // 触发。radius 单位是米；0 表示"用系统默认"而不是零米。
                let structured = EKStructuredLocation(title: place.name)
                structured.geoLocation = CLLocation(
                    latitude: place.latitude, longitude: place.longitude)
                structured.radius = place.radiusMeters
                let alarm = EKAlarm()
                alarm.structuredLocation = structured
                alarm.proximity = place.proximity == "leave"
                    ? .leave : .enter
                reminder.addAlarm(alarm)
                reminder.location = place.name
            }
            do {
                try eventStore.save(reminder, commit: false)
                map[item.id] = reminder.calendarItemIdentifier
            } catch {
                // 存不进去必须留痕：用户以为提醒建好了，实际苹果那边
                // 一条都没有。原来这里是裸 continue —— 与成功无法区分。
                failures += 1
                continue
            }
        }
        do {
            try eventStore.commit()
        } catch {
            // commit 失败 = 这一轮**全部**提醒都没落地。以前它被
            // try? 吞掉，回执还报 projected。
            defaults?.set(map, forKey: mapKey)
            return (resolved, "commit-failed")
        }
        defaults?.set(map, forKey: mapKey)
        return (resolved,
                failures > 0 ? "partial:" + String(failures) : "projected")
    }

    private func ensureCalendar() -> EKCalendar? {
        if let saved = defaults?.string(forKey: calendarKey),
           let existing = eventStore.calendar(withIdentifier: saved) {
            return existing
        }
        if let found = eventStore.calendars(for: .reminder)
            .first(where: { $0.title == Self.calendarTitle }) {
            defaults?.set(found.calendarIdentifier, forKey: calendarKey)
            return found
        }
        let calendar = EKCalendar(for: .reminder, eventStore: eventStore)
        calendar.title = Self.calendarTitle
        calendar.source = eventStore.defaultCalendarForNewReminders()?.source
            ?? eventStore.sources.first
        do {
            try eventStore.saveCalendar(calendar, commit: true)
            defaults?.set(calendar.calendarIdentifier, forKey: calendarKey)
            return calendar
        } catch {
            return nil
        }
    }
}
