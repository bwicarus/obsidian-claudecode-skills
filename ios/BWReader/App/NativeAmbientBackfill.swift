import Foundation

/// 逐段重转的后台队列（2026-09-26 用户：「逐段转写应该在空闲时后台处理」）。
///
/// 主线连续识别已经实时给出文字；逐段重转（说别的语言的人用他的语言重转、推测语言）只是事后纠正，
/// 不必抢实时。所以切出来的段连同音频先落盘排队，等周围安静（主线几秒没新字）再一段一段转，
/// 转完修正记录：还在本机没送出的窗口就地替换，已经送到服务器的走 /api/ambient/revise。
///
/// 落盘：Caches/BWReader/ambient-backfill/<id>.pcm（16 kHz Int16）+ jobs.json。App 重启也接着转。
/// ⚠ 只在旁听管线的 work 队列上用（不是线程安全的）。
final class NativeAmbientBackfill {
    struct Job: Codable, Equatable {
        let id: String
        let slotKey: String
        let speaker: Int
        let session: String          // 分离器会话（= slotKey 的前半），用于判断还能不能就地改本机窗口
        let t0: Double               // 绝对时间（epoch 毫秒）：服务器时间轴按它找要替换的句子
        let t1: Double
        let streamStart: Double      // 管线时间轴：就地改本机窗口用
        let streamEnd: Double
        let locale: String?          // nil = 推测语言
        let confirmed: Bool
        var attempts: Int
    }

    static let maxJobs = 300

    private(set) var jobs: [Job] = []
    private let folder: URL
    private var index: URL { folder.appendingPathComponent("jobs.json") }

    init() {
        folder = (FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
                  ?? FileManager.default.temporaryDirectory)
            .appendingPathComponent("BWReader/ambient-backfill", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        if let data = try? Data(contentsOf: index), let saved = try? JSONDecoder().decode([Job].self, from: data) {
            jobs = saved.filter { FileManager.default.fileExists(atPath: audioURL($0.id).path) }
        }
    }

    var count: Int { jobs.count }

    func audioURL(_ id: String) -> URL { folder.appendingPathComponent(id + ".pcm") }

    /// 入队。满了丢最老的（出声）。
    func add(_ job: Job, samples: [Int16]) {
        do {
            try samples.withUnsafeBufferPointer { Data(buffer: $0) }.write(to: audioURL(job.id), options: .atomic)
        } catch {
            NativeAmbientLog.note("逐段重转：音频存不下 \(error.localizedDescription)，这段跳过", level: "error")
            return
        }
        jobs.append(job)
        if jobs.count > Self.maxJobs {
            let dropped = jobs.removeFirst()
            try? FileManager.default.removeItem(at: audioURL(dropped.id))
            NativeAmbientLog.note("逐段重转：队列满 \(Self.maxJobs) 段，丢掉最老的一段", level: "error")
        }
        save()
    }

    func next() -> Job? { jobs.first }

    func samples(of job: Job) -> [Float]? {
        guard let data = try? Data(contentsOf: audioURL(job.id)), data.count >= 2 else { return nil }
        return data.withUnsafeBytes { raw in raw.bindMemory(to: Int16.self).map { Float($0) / 32_768 } }
    }

    func finish(_ job: Job) {
        jobs.removeAll { $0.id == job.id }
        try? FileManager.default.removeItem(at: audioURL(job.id))
        save()
    }

    /// 失败的放回队尾，最多试 3 次。
    func retry(_ job: Job) {
        jobs.removeAll { $0.id == job.id }
        var again = job
        again.attempts += 1
        if again.attempts >= 3 {
            try? FileManager.default.removeItem(at: audioURL(job.id))
            NativeAmbientLog.note("逐段重转：\(job.slotKey) 一段试了 3 次都没成，放弃", level: "error")
        } else {
            jobs.append(again)
        }
        save()
    }

    private func save() {
        if let data = try? JSONEncoder().encode(jobs) { try? data.write(to: index, options: .atomic) }
    }
}
