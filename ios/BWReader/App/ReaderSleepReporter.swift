import Foundation
import HealthKit

/// 从健康库读昨晚的睡眠，把「今早几点醒的」报给 Windows（2026-09-08 用户要做手表功能）。
///
/// 为什么在 iPhone 读而不是手表：睡眠分段由用户自己的软件写进健康库，而健康数据在
/// iPhone 与手表之间本就同步。从 iPhone 读省掉手表 target 的权限、后台限制和一整套传输。
///
/// 为什么值得读：Windows 侧本来就能从「设备操作账本里的那段静默」推断起床，但那只能看出
/// 「他开始碰这套系统了」。健康数据知道他其实七点就醒、只是先用了别的软件 —— 这正是
/// 复习提醒该不该在早上出声的判断依据。
///
/// 失败一律安静降级：没授权、没数据、网络不通，Windows 那边自动回落到活动推断。
/// 但**每一种失败都写进 lastNote**，不让它变成"什么都没发生也不知道为什么"。
@MainActor
final class ReaderSleepReporter {
    static let shared = ReaderSleepReporter()

    /// 最近一次尝试的结果。失败原因要留得住 —— 这条链没有界面，出问题只能靠它。
    private(set) var lastNote: String = "尚未运行"
    private var lastReportedWake: Date?
    private var running = false

    private let store = HKHealthStore()
    private static let endpoint = URL(
        string: "https://\(ReaderNativePiGateway.piHost)/reader-sleep/v1")

    private init() {}

    /// App 进前台时调一次。同一个醒来时刻只报一次，重复调用是廉价的。
    func refresh() async {
        guard !running else { return }
        running = true
        defer { running = false }

        guard HKHealthStore.isHealthDataAvailable() else {
            lastNote = "这台设备没有健康数据"
            return
        }
        guard let sleepType = HKCategoryType.categoryType(
            forIdentifier: .sleepAnalysis) else {
            lastNote = "取不到睡眠数据类型"
            return
        }
        do {
            // 只读、不写：toShare 传空集合。用户在系统弹窗里拒绝时这里不会抛错，
            // 抛错的是后面的查询 —— HealthKit 刻意不告诉 App「你被拒了」以免暴露隐私。
            try await store.requestAuthorization(toShare: [], read: [sleepType])
        } catch {
            lastNote = "请求健康授权失败：\(error.localizedDescription)"
            return
        }

        guard let woke = await lastWakeTime(sleepType) else {
            // 拿不到样本最常见的原因就是没授权，而 HealthKit 不会明说，所以这句要含糊得诚实。
            lastNote = "读不到昨晚的睡眠记录（可能未授权，或健康库里没有数据）"
            return
        }
        guard Calendar.current.isDateInToday(woke) else {
            lastNote = "最近一次醒来不在今天，不上报"
            return
        }
        if let reported = lastReportedWake,
           abs(reported.timeIntervalSince(woke)) < 60 {
            lastNote = "同一个醒来时刻已报过"
            return
        }
        if await send(woke: woke) {
            lastReportedWake = woke
        }
    }

    /// 过去 36 小时里最后一段「睡着」的结束时刻 = 醒来。
    ///
    /// 只认真正睡着的样本，不认「在床上」：躺着刷手机也会被记成 inBed，
    /// 拿它当醒来会把起床点算到躺下的那一刻。
    private func lastWakeTime(_ sleepType: HKCategoryType) async -> Date? {
        let since = Date().addingTimeInterval(-36 * 3600)
        let predicate = HKQuery.predicateForSamples(
            withStart: since, end: Date(), options: .strictEndDate)
        let sort = NSSortDescriptor(
            key: HKSampleSortIdentifierEndDate, ascending: false)
        return await withCheckedContinuation { continuation in
            let query = HKSampleQuery(
                sampleType: sleepType,
                predicate: predicate,
                limit: 200,
                sortDescriptors: [sort]
            ) { _, samples, _ in
                let asleep = (samples as? [HKCategorySample] ?? [])
                    .filter { Self.isAsleep($0.value) }
                continuation.resume(returning: asleep.first?.endDate)
            }
            store.execute(query)
        }
    }

    /// iOS 16 起睡眠分成 core/deep/REM 三档，之前只有一个 asleep。
    /// 两套都要认，否则在任一系统版本上都会静默地一条样本都匹配不到。
    private static func isAsleep(_ value: Int) -> Bool {
        if #available(iOS 16.0, *) {
            let asleepValues: Set<Int> = [
                HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,
                HKCategoryValueSleepAnalysis.asleepCore.rawValue,
                HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
                HKCategoryValueSleepAnalysis.asleepREM.rawValue,
            ]
            return asleepValues.contains(value)
        }
        return value == HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue
    }

    private func send(woke: Date) async -> Bool {
        guard let endpoint = Self.endpoint else {
            lastNote = "睡眠上报地址无效"
            return false
        }
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 12
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "wokeAtMs": Int(woke.timeIntervalSince1970 * 1000),
            "source": "healthkit",
        ])
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let payload = (try? JSONSerialization.jsonObject(with: data))
                as? [String: Any] ?? [:]
            guard status == 200, payload["ok"] as? Bool == true else {
                // 桥的 detail 说得清错在哪个字段 —— 原样留住，别折成"上报失败"。
                lastNote = "上报被拒：" + ((payload["detail"] as? String)
                    ?? "HTTP \(status)")
                return false
            }
            lastNote = "已上报醒来时刻 " + Self.stamp(woke)
            return true
        } catch {
            lastNote = "上报失败（电脑不可达？）：\(error.localizedDescription)"
            return false
        }
    }

    private static func stamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "MM-dd HH:mm"
        return formatter.string(from: date)
    }
}
