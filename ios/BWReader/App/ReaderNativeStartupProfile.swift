import Foundation

/// 启动耗时与内存基线。
///
/// ⚠ 它存在的唯一理由：**在动"把判断搬进 Swift"这种大刀之前先有数字**。
/// 2026-09-23 用户问"搬了性能会提升么"，我当时只能估 —— 而前一天真正的卡顿
/// 四个原因里有两个是我自己在原生那边造成的（移动的视图套 Liquid Glass、
/// 落点预览发布到主模型），跟 JS 快慢无关。没有基线，改完也说不清有没有用。
///
/// ⚠ 跟故障现场同一条规矩：**不碰阅读页**。只读进程自己的内存和时钟，
/// 任何一步都不 await、不调网页 —— 页面死掉时它照样有数。
@MainActor
final class ReaderNativeStartupProfile: ObservableObject {
    static let shared = ReaderNativeStartupProfile()

    struct Mark: Identifiable {
        let id = UUID()
        let name: String
        /// 距进程启动的秒数。
        let sinceLaunch: TimeInterval
        /// 距上一个标记的秒数 —— 看"哪一段贵"要的是这个，不是累计值。
        let delta: TimeInterval
        let footprintMB: Int
    }

    @Published private(set) var marks: [Mark] = []
    private let launchedAt = Date()

    private init() {}

    /// 打一个点。同名只记第一次 —— 启动阶段会被重复触发（重载、回前台），
    /// 记后面的会把基线冲掉。
    func mark(_ name: String) {
        guard !marks.contains(where: { $0.name == name }) else { return }
        let now = Date().timeIntervalSince(launchedAt)
        marks.append(Mark(name: name,
                          sinceLaunch: now,
                          delta: now - (marks.last?.sinceLaunch ?? 0),
                          footprintMB: Self.footprintMB()))
    }

    /// 当前进程的物理内存占用（MB）。0 = 取不到。
    ///
    /// ⚠ 唯一实现放这里：ReaderWebView 的崩溃记录也从这儿取，
    /// 免得两处各写一份、日后报出两个对不上的数。
    static func footprintMB() -> Int {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(
            MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return 0 }
        return Int(info.phys_footprint / (1024 * 1024))
    }

    var readable: String {
        guard !marks.isEmpty else { return "（本次启动还没有记录）" }
        return marks.map { mark in
            String(format: "%6.2fs  +%5.2fs  %4dMB  %@",
                   mark.sinceLaunch, mark.delta, mark.footprintMB, mark.name)
        }.joined(separator: "\n")
    }
}
