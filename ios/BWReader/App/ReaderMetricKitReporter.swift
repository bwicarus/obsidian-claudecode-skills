import Foundation
import MetricKit

/// 苹果 MetricKit 的诊断与资源用量 → Mac 服务器（`/reader-error-log/metrickit`）。
///
/// 为什么要有它（2026-09-25）：闪退、发热、磁盘写入超限这些证据 iOS 其实都记着，
/// 但只在设备上 —— 今天 18 GB device 库、974 的四类崩溃，全是连着线手动拉 .ips 才看见的。
/// 用户说「烫」「闪退」时，证据应该已经躺在服务器上了。
///
/// - 诊断包（崩溃 / 卡死 / CPU 超限 / 磁盘写入超限）：系统通常在下次启动或约一天内送达。
/// - 资源用量（每日：CPU 时间、耗电相关、磁盘写入量、内存峰值等）：约每 24 小时一包。
/// - 先落盘再上传：服务器没开 / 网络不通时攒着，下次启动或下一包到时补送，送成才删。
final class ReaderMetricKitReporter: NSObject, MXMetricManagerSubscriber, @unchecked Sendable {  // 状态全在 queue 上改
    static let shared = ReaderMetricKitReporter()

    private let queue = DispatchQueue(label: "space.bwicarus.reader.metrickit")
    private var started = false
    private var sending = false
    private var origin = ""

    private var outbox: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("BWReader/metrickit-outbox", isDirectory: true)
    }

    /// App 进前台时调（与故障上报同一时机、同一服务器地址）。重复调用只补送积压。
    func start(origin: String) {
        queue.async { [self] in
            self.origin = origin
            if !started {
                started = true
                MXMetricManager.shared.add(self)
                // 订阅之前系统已经攒下的诊断：闪退那一次的包往往就在这里。
                store(MXMetricManager.shared.pastDiagnosticPayloads.map { ("diagnostic", $0.jsonRepresentation()) })
            }
            flush()
        }
    }

    func didReceive(_ payloads: [MXDiagnosticPayload]) {
        queue.async { [self] in
            store(payloads.map { ("diagnostic", $0.jsonRepresentation()) })
            flush()
        }
    }

    func didReceive(_ payloads: [MXMetricPayload]) {
        queue.async { [self] in
            store(payloads.map { ("metrics", $0.jsonRepresentation()) })
            flush()
        }
    }

    private func store(_ items: [(kind: String, data: Data)]) {
        guard !items.isEmpty else { return }
        try? FileManager.default.createDirectory(at: outbox, withIntermediateDirectories: true)
        for item in items {
            let name = String(format: "%.0f", Date().timeIntervalSince1970 * 1000) + "-" + item.kind
                + "-" + UUID().uuidString.prefix(6) + ".json"
            try? item.data.write(to: outbox.appendingPathComponent(name), options: .atomic)
        }
        // 上限：攒太多说明服务器长期不可达，丢最旧的，别让发件箱自己变成下一个 18 GB。
        let files = (try? FileManager.default.contentsOfDirectory(at: outbox, includingPropertiesForKeys: nil)) ?? []
        for stale in files.sorted { $0.lastPathComponent > $1.lastPathComponent }.dropFirst(100) {
            try? FileManager.default.removeItem(at: stale)
        }
    }

    private func flush() {
        guard !sending, !origin.isEmpty else { return }
        let files = ((try? FileManager.default.contentsOfDirectory(at: outbox, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
        guard !files.isEmpty else { return }
        sending = true
        Task.detached(priority: .utility) { [self, origin] in
            for file in files {
                let kind = file.lastPathComponent.contains("-metrics-") ? "metrics" : "diagnostic"
                guard let data = try? Data(contentsOf: file),
                      let url = URL(string: origin + "/reader-error-log/metrickit?kind=" + kind) else { continue }
                var request = URLRequest(url: url)
                request.httpMethod = "POST"
                request.timeoutInterval = 20
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.setValue(origin, forHTTPHeaderField: "Origin")
                request.httpBody = data
                guard let (_, reply) = try? await URLSession.shared.data(for: request),
                      (200...299).contains((reply as? HTTPURLResponse)?.statusCode ?? 0) else { break }
                try? FileManager.default.removeItem(at: file)
            }
            queue.async { self.sending = false }
        }
    }
}
