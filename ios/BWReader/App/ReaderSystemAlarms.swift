import Foundation
import SwiftUI

#if canImport(AlarmKit)
import AlarmKit
#endif

/// 系统闹钟投影（AlarmKit，iPadOS 26+）。
///
/// 为什么需要它：本地通知到点会响，但**静音键一按就没了**；而"20:12 的
/// 电车""该出门了"这类提醒的价值恰恰在于必须叫住人。AlarmKit 是 Apple
/// 给第三方 App 的系统级闹钟通道 —— 官方明文：**压过专注模式与静音**，
/// 有配对 Apple Watch 时同步转发，而且排上之后 App 关着照样响。
///
/// 三条提醒通道的分工（都排，互为兜底，各自的失效场景不重叠）：
///   1. 到点本地通知（ReaderSystemProjection）：全系统版本可用，被静音键克制；
///   2. 苹果提醒事项 + 闹钟：依赖提醒事项权限，好处是能在提醒 App 里勾完成；
///   3. **系统闹钟（本文件）**：只有 iPadOS 26+ 有，但它是唯一"必然叫醒"的。
///
/// 版本兼容：SDK 没有 AlarmKit（老 Xcode）→ 整块编译不进去；系统低于 26
/// → 运行时跳过。两种情况都如实返回状态串，由投影回执带回去 —— 缺席要
/// 出声，绝不静默假装排了闹钟。
///
/// ⚠ API 事实全部来自 Apple DocC 符号图谱实取（2026-08-26），三条反直觉的：
///   · **没有 AlarmKit entitlement**（官方 entitlement 索引里零命中），
///     唯一的门是 Info.plist 的 `NSAlarmKitUsageDescription`；
///   · Widget extension **只在用 countdown 呈现时**才必需，我们用的是
///     固定时刻 + 纯 alert 呈现，不触发那个条件；
///   · `stopIntent` 可选 —— 按钮的停止/贪睡由 AlarmManager 自己处理。
final class ReaderSystemAlarms {
    static let shared = ReaderSystemAlarms()

    private let defaults = UserDefaults(
        suiteName: ReaderNativeBridgeContract.appGroupIdentifier)
    /// 通知 id → ["alarmId": UUID 串, "dueAtMs": 到点时刻]。
    private let mapKey = "bwAlarmProjectionMapV1"
    /// 一次最多排几个系统闹钟。闹钟比通知更打扰，宁可少；AlarmKit 自身
    /// 也有上限（超了抛 maximumLimitReached，具体数字 Apple 没公开）。
    private static let maximumAlarms = 8

    /// 返回状态串（进投影回执的 revision）：
    /// scheduled=N / denied / unsupported-os / sdk-unavailable / failed:<原因>
    func sync(_ items: [ReaderSystemProjection.Item]) async -> String {
        #if canImport(AlarmKit)
        if #available(iOS 26.0, *) {
            return await scheduleWithAlarmKit(items)
        }
        return "unsupported-os"
        #else
        return "sdk-unavailable"
        #endif
    }

    /// 本轮该排哪些闹钟：带 dueAt 且尚未到点的条目，按时刻取最近的几个。
    /// 与本地通知排程共用同一批条目 —— 两条通道都排，谁先响都行。
    fileprivate func upcoming(
        _ items: [ReaderSystemProjection.Item]
    ) -> [(item: ReaderSystemProjection.Item, dueAtMs: Int64)] {
        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        return items
            .compactMap { item in
                guard let at = item.dueAtMs, at > nowMs else { return nil }
                return (item: item, dueAtMs: at)
            }
            .sorted { $0.dueAtMs < $1.dueAtMs }
            .prefix(Self.maximumAlarms)
            .map { $0 }
    }

    fileprivate func loadMap() -> [String: [String: Any]] {
        (defaults?.dictionary(forKey: mapKey) as? [String: [String: Any]])
            ?? [:]
    }

    fileprivate func saveMap(_ value: [String: [String: Any]]) {
        defaults?.set(value, forKey: mapKey)
    }
}

#if canImport(AlarmKit)

/// 空 metadata：我们只用固定时刻的告警呈现，不需要携带业务数据。
/// `AlarmMetadata` 自身没有要求，空结构体的一致性由编译器全部合成。
@available(iOS 26.0, *)
struct ReaderAlarmMetadata: AlarmMetadata {}

@available(iOS 26.0, *)
extension ReaderSystemAlarms {
    fileprivate func scheduleWithAlarmKit(
        _ items: [ReaderSystemProjection.Item]
    ) async -> String {
        let manager = AlarmManager.shared
        var authorization = manager.authorizationState
        if authorization == .notDetermined {
            authorization = (try? await manager.requestAuthorization())
                ?? .denied
        }
        guard authorization == .authorized else { return "denied" }

        let wanted = upcoming(items)
        var map = loadMap()

        // 撤销：条目已消失，或到点时刻被改过（改了就必须重排，
        // 否则闹钟停在旧时刻 —— 比不响更糟）。
        for (notificationId, record) in map {
            let live = wanted.first { $0.item.id == notificationId }
            let recordedDue = (record["dueAtMs"] as? NSNumber)?.int64Value
            guard live == nil || live?.dueAtMs != recordedDue else { continue }
            if let raw = record["alarmId"] as? String,
               let alarmId = UUID(uuidString: raw) {
                try? manager.cancel(id: alarmId)
            }
            map.removeValue(forKey: notificationId)
        }

        var scheduled = 0
        var failure: String?
        for entry in wanted {
            // 已排且时刻未变 —— 不重排（重排会换掉 id，多余的抖动）。
            if map[entry.item.id] != nil {
                scheduled += 1
                continue
            }
            let alert = AlarmPresentation.Alert(
                title: LocalizedStringResource(
                    stringLiteral: entry.item.title),
                stopButton: AlarmButton(
                    text: "知道了",
                    textColor: .white,
                    systemImageName: "checkmark.circle"))
            let attributes = AlarmAttributes<ReaderAlarmMetadata>(
                presentation: AlarmPresentation(alert: alert),
                tintColor: Color.orange)
            let configuration = AlarmManager
                .AlarmConfiguration<ReaderAlarmMetadata>.alarm(
                    schedule: .fixed(Date(
                        timeIntervalSince1970: Double(entry.dueAtMs) / 1000)),
                    attributes: attributes)
            let alarmId = UUID()
            do {
                _ = try await manager.schedule(
                    id: alarmId, configuration: configuration)
                map[entry.item.id] = [
                    "alarmId": alarmId.uuidString,
                    "dueAtMs": NSNumber(value: entry.dueAtMs),
                ]
                scheduled += 1
            } catch {
                // 排不上要留下原因：用户以为会被叫醒，实际没排上，
                // 这种沉默是最贵的（通道自身的失败必须能被看见）。
                failure = String(String(describing: error).prefix(60))
            }
        }
        saveMap(map)
        if let failure {
            return "failed:" + failure
        }
        return "scheduled=" + String(scheduled)
    }
}

#endif
