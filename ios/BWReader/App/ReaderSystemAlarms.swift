import Foundation

#if canImport(AlarmKit)
import AlarmKit
#endif

/// 系统闹钟投影（AlarmKit，iPadOS 26+）。
///
/// 为什么需要它：本地通知到点会响，但**静音键一按就没了**；而"20:12 的
/// 电车""该出门了"这类提醒的价值恰恰在于必须叫住人。AlarmKit 是 Apple
/// 给第三方 App 的系统级闹钟通道 —— 全屏、绕过静音与专注模式、可贪睡，
/// 而且**排上之后 App 关着照样响**。
///
/// 三条提醒通道的分工（都排，互为兜底，各自的失效场景不重叠）：
///   1. 本地通知（本文件之外，ReaderSystemProjection）：全系统版本可用，
///      被静音键克制；
///   2. 苹果提醒事项 + 闹钟：依赖提醒事项权限，好处是能在提醒 App 里勾完成；
///   3. **系统闹钟（本文件）**：只有 iPadOS 26+ 有，但它是唯一"必然叫醒"的。
///
/// 版本兼容：SDK 没有 AlarmKit（老 Xcode）→ 整块编译不进去；系统低于 26
/// → 运行时跳过。两种情况都如实返回状态串，由投影回执带回去 —— 缺席要
/// 出声，绝不静默假装排了闹钟。
final class ReaderSystemAlarms {
    static let shared = ReaderSystemAlarms()

    private let defaults = UserDefaults(
        suiteName: ReaderNativeBridgeContract.appGroupIdentifier)
    /// 通知 id → 已排闹钟的 UUID + 到点时刻（毫秒）。
    private let mapKey = "bwAlarmProjectionMapV1"
    /// 一次最多排几个系统闹钟。闹钟比通知更打扰，宁可少。
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
