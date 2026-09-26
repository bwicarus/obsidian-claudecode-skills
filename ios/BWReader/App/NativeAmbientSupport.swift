import AVFoundation
import FluidAudio
import Foundation
import os

// 环境旁听与嘈杂环境人声隔离共用的零件（2026-09-26）。
//
//   NativeAmbientLog        诊断出口：本机面板 + 故障面包屑 + 服务器 client-log（每个提前退出都出声）
//   NativeAmbientServer     带设备令牌打「我的服务器」的 /api/ambient/*（服务器持 jev 密钥，App 不持有）
//   NativeSortformerModels  FluidAudio Sortformer 模型（首次用时从 HuggingFace 下载，之后本机缓存）
//   NativeStreamDiarizer    流式说话人分离 + 用户声纹（登记的那个人 = 「我」）
//   NativeVoiceprint        用户声纹：朗读 10 秒存成 16 kHz 单声道
//   NativeResampler16k      任意采样率 → 16 kHz（Sortformer 的输入）

// MARK: - 诊断出口

@MainActor
final class NativeAmbientLog: ObservableObject {
    static let shared = NativeAmbientLog()

    @Published private(set) var lines: [String] = []

    private let logger = Logger(subsystem: "space.bwicarus.bwreader2", category: "ambient")
    private var outbox: [[String: String]] = []
    private var flushTask: Task<Void, Never>?
    private let maxLines = 200

    private init() {}

    /// 任意线程可调。
    nonisolated static func note(_ text: String, level: String = "info") {
        Task { @MainActor in shared.append(text, level: level) }
    }

    private func append(_ text: String, level: String) {
        let stamp = Self.clock.string(from: Date())
        lines.append("\(stamp) \(text)")
        if lines.count > maxLines { lines.removeFirst(lines.count - maxLines) }
        if level == "error" { logger.error("\(text, privacy: .public)") }
        else { logger.info("\(text, privacy: .public)") }
        ReaderNativeFaultReporter.shared.note("ambient", text)
        outbox.append(["t": ISO8601DateFormatter().string(from: Date()), "level": level,
                       "msg": "[ambient] " + String(text.prefix(1500))])
        if outbox.count > 200 { outbox.removeFirst(outbox.count - 200) }
        scheduleFlush()
    }

    /// 攒 5 秒一批送 client-log（与网页侧 dlog 同一个文件）。送不出去就留着下次再送，
    /// 但诊断通道绝不反过来打断被诊断的功能。
    private func scheduleFlush() {
        guard flushTask == nil else { return }
        flushTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard let self else { return }
            let batch = self.outbox
            self.flushTask = nil
            guard !batch.isEmpty else { return }
            let body: [String: Any] = ["device": "ios-native", "surface": "native-ambient",
                                       "build": nativeAppBuildVersion, "lines": batch]
            if (try? await NativeAmbientServer.post("pdf/api/client-log", body: body, timeout: 8)) != nil {
                self.outbox.removeFirst(min(batch.count, self.outbox.count))
            } else {
                self.logger.error("client-log delivery failed; \(batch.count) lines kept")
            }
        }
    }

    private static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()
}

// MARK: - 服务器

enum NativeAmbientServer {
    enum Failure: LocalizedError {
        case notLoggedIn
        case badURL
        case http(Int, String)
        case invalidResponse

        var errorDescription: String? {
            switch self {
            case .notLoggedIn: return "App 还没登录服务器（没有设备令牌）"
            case .badURL: return "服务器地址拼不出来"
            case .http(let code, let body): return "HTTP \(code) \(body.prefix(160))"
            case .invalidResponse: return "服务器回的不是 JSON"
            }
        }
    }

    /// ⚠ 不发 Origin 头（同 ReaderVoipTokenUpload 的教训）；鉴权只用设备令牌。
    static func post(_ path: String, body: [String: Any], timeout: TimeInterval = 20) async throws -> [String: Any] {
        var request = try authorizedRequest(path, timeout: timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(request)
    }

    static func get(_ path: String, timeout: TimeInterval = 15) async throws -> [String: Any] {
        var request = try authorizedRequest(path, timeout: timeout)
        request.httpMethod = "GET"
        return try await send(request)
    }

    private static func authorizedRequest(_ path: String, timeout: TimeInterval) throws -> URLRequest {
        guard let stored = try? ReaderAccountTokenStore.shared.loadIfPresent() else { throw Failure.notLoggedIn }
        guard let url = ReaderServer.url(path) else { throw Failure.badURL }
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        request.httpShouldHandleCookies = false
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer " + stored.token, forHTTPHeaderField: "Authorization")
        return request
    }

    private static func send(_ request: URLRequest) async throws -> [String: Any] {
        let (data, response) = try await URLSession.shared.data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? -1
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (200..<300).contains(code) else {
            let detail = (object?["code"] as? String) ?? String(decoding: data.prefix(160), as: UTF8.self)
            throw Failure.http(code, detail)
        }
        guard let object else { throw Failure.invalidResponse }
        return object
    }
}

// MARK: - Sortformer 模型

/// 两处（通话降噪、环境旁听）共用一份已加载模型。同一时间只会有一处在跑
/// （旁听在通话期间暂停），但即使并存，各自的 SortformerDiarizer 状态是独立的。
actor NativeSortformerModels {
    static let shared = NativeSortformerModels()
    /// 最低延迟的流式配置（约 1.04 s 定稿延迟；暂定结果更早）。
    static let config = SortformerConfig.fastV2_1

    private var cached: SortformerModels?
    private var loading: Task<SortformerModels, Error>?

    func models() async throws -> SortformerModels {
        if let cached { return cached }
        if let loading { return try await loading.value }
        NativeAmbientLog.note("说话人分离模型：开始加载（首次需要从 HuggingFace 下载）")
        let started = Date()
        let task = Task { try await SortformerModels.loadFromHuggingFace(config: Self.config) }
        loading = task
        do {
            let models = try await task.value
            cached = models
            loading = nil
            NativeAmbientLog.note(String(format: "说话人分离模型：已就绪（%.1f 秒）", Date().timeIntervalSince(started)))
            return models
        } catch {
            loading = nil
            NativeAmbientLog.note("说话人分离模型加载失败：\(error.localizedDescription)", level: "error")
            throw error
        }
    }
}

// MARK: - 声纹特征比对

/// 用 FluidAudio 的 WeSpeaker 嵌入模型（256 维，L2 归一化）给一段声音算声纹特征，
/// 再和「我」及所有熟人的特征比余弦距离（1 − cos）。人数不设上限，不占分离器槽位。
///
/// 阈值是起点而不是定论：每次比对都把最近 / 次近距离写进日志，照实测再调。
actor NativeSpeakerEmbedder {
    static let shared = NativeSpeakerEmbedder()
    /// 最近距离低于它才算认出。
    static let matchDistance: Float = 0.55
    /// 最近与次近至少要差这么多，否则算「分不清」（两位熟人声音很像时宁可不标）。
    static let ambiguityMargin: Float = 0.08
    static let chunkSamples = 160_000           // 模型窗口 10 秒
    static let minimumSamples = 48_000          // 3 秒

    struct Match: Sendable {
        let name: String?
        let nearest: String?
        let distance: Float
        let runnerUp: Float
    }

    private var manager: DiarizerManager?
    private var loading: Task<DiarizerModels, Error>?
    private var cache: [String: (stamp: Date, vector: [Float])] = [:]
    private var userCache: (stamp: Date, vector: [Float])?

    private func ready() async throws -> DiarizerManager {
        if let manager { return manager }
        let task: Task<DiarizerModels, Error>
        if let loading { task = loading } else {
            NativeAmbientLog.note("声纹比对模型：开始加载（首次需要从 HuggingFace 下载）")
            task = Task { try await DiarizerModels.downloadIfNeeded() }
            loading = task
        }
        do {
            let models = try await task.value
            loading = nil
            if let manager { return manager }
            let created = DiarizerManager()
            created.initialize(models: models)
            manager = created
            NativeAmbientLog.note("声纹比对模型：已就绪")
            return created
        } catch {
            loading = nil
            NativeAmbientLog.note("声纹比对模型加载失败：\(error.localizedDescription)", level: "error")
            throw error
        }
    }

    /// 16 kHz 单声道 → 归一化声纹特征。超过 10 秒按 10 秒一段分别算再平均（最后一段不足 3 秒就丢掉）。
    func embedding(of samples: [Float]) async throws -> [Float] {
        let manager = try await ready()
        var sum: [Float] = []
        var count = 0
        var start = 0
        while start < samples.count {
            let end = min(samples.count, start + Self.chunkSamples)
            if end - start >= Self.minimumSamples || (count == 0 && end == samples.count) {
                let vector = try manager.extractSpeakerEmbedding(from: Array(samples[start..<end]))
                if sum.isEmpty { sum = vector } else { for i in 0..<min(sum.count, vector.count) { sum[i] += vector[i] } }
                count += 1
            }
            start = end
        }
        guard count > 0 else { throw NSError(domain: "BWSpeaker", code: 1, userInfo: [NSLocalizedDescriptionKey: "声音太短"]) }
        return Self.normalized(sum)
    }

    /// 和「我」及全部熟人比。
    func identify(_ vector: [Float]) async -> Match {
        var scored: [(String, Float)] = []
        if let user = await userVector() { scored.append(("我", Self.distance(vector, user))) }
        for person in NativeVoiceprint.people() {
            guard let reference = await personVector(person) else { continue }
            scored.append((person.name, Self.distance(vector, reference)))
        }
        scored.sort { $0.1 < $1.1 }
        guard let best = scored.first else { return Match(name: nil, nearest: nil, distance: 2, runnerUp: 2) }
        // 同名（同一人多个来源）不算次近
        let runnerUp = scored.first { $0.0 != best.0 }?.1 ?? 2
        let accepted = best.1 < Self.matchDistance && runnerUp - best.1 >= Self.ambiguityMargin
        return Match(name: accepted ? best.0 : nil, nearest: best.0, distance: best.1, runnerUp: runnerUp)
    }

    private func personVector(_ person: NativeVoiceprint.Person) async -> [Float]? {
        if let hit = cache[person.id], hit.stamp == person.updatedAt { return hit.vector }
        guard let samples = NativeVoiceprint.loadPerson(person.id) else { return nil }
        do {
            let vector = try await embedding(of: samples)
            cache[person.id] = (person.updatedAt, vector)
            return vector
        } catch {
            NativeAmbientLog.note("声纹比对：\(person.name) 的特征算不出来 \(error.localizedDescription)", level: "error")
            return nil
        }
    }

    private func userVector() async -> [Float]? {
        let stamp = (try? FileManager.default.attributesOfItem(atPath: NativeVoiceprint.fileURL.path)[.modificationDate]) as? Date
        guard let stamp else { userCache = nil; return nil }
        if let userCache, userCache.stamp == stamp { return userCache.vector }
        guard let samples = NativeVoiceprint.load(), let vector = try? await embedding(of: samples) else { return nil }
        userCache = (stamp, vector)
        return vector
    }

    static func normalized(_ vector: [Float]) -> [Float] {
        let norm = sqrt(vector.reduce(0) { $0 + $1 * $1 })
        return norm > 0 ? vector.map { $0 / norm } : vector
    }

    static func distance(_ a: [Float], _ b: [Float]) -> Float {
        guard a.count == b.count, !a.isEmpty else { return 2 }
        var dot: Float = 0, na: Float = 0, nb: Float = 0
        for i in 0..<a.count { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i] }
        guard na > 0, nb > 0 else { return 2 }
        return 1 - dot / (sqrt(na) * sqrt(nb))
    }
}

// MARK: - 流式说话人分离

/// ⚠ 不是线程安全的：调用方把它的全部调用放在同一条串行队列上。
final class NativeStreamDiarizer {
    static let sampleRate: Double = 16_000

    let diarizer: SortformerDiarizer
    /// 登记声纹对应的说话人槽位；没登记声纹时为 nil（调用方按「说话最多的人」兜底）。
    /// 熟人不占槽位：分离器只回答「哪几段是同一个人」，是谁由 NativeSpeakerEmbedder 做声纹比对。
    private(set) var userIndex: Int?
    private(set) var fedSamples = 0

    init(models: SortformerModels, voiceprint: [Float]?) throws {
        diarizer = SortformerDiarizer(config: NativeSortformerModels.config)
        diarizer.initialize(models: models)
        if let voiceprint, !voiceprint.isEmpty {
            userIndex = try diarizer.enrollSpeaker(withAudio: voiceprint, sourceSampleRate: nil, named: "我")?.index
        }
    }

    /// 已送入的音频时长（秒，从本实例创建算起）。
    var elapsed: Double { Double(fedSamples) / Self.sampleRate }

    /// 送 16 kHz 单声道；够一个分块时模型才会真正跑。
    @discardableResult
    func feed(_ samples: [Float]) throws -> DiarizerTimelineUpdate? {
        fedSamples += samples.count
        return try diarizer.process(samples: samples, sourceSampleRate: nil)
    }

    private func segments() -> [DiarizerSegment] {
        diarizer.timeline.speakers.values.flatMap { $0.finalizedSegments + $0.tentativeSegments }
    }

    /// 最近 `window` 秒里，说话累计超过 `minSpeech` 秒的说话人。
    func activeSpeakers(within window: Double, minSpeech: Double) -> [Int] {
        let from = elapsed - window
        var talk: [Int: Double] = [:]
        for segment in segments() where Double(segment.endTime) > from {
            let start = max(Double(segment.startTime), from)
            talk[segment.speakerIndex, default: 0] += max(0, Double(segment.endTime) - start)
        }
        return talk.filter { $0.value >= minSpeech }.map(\.key).sorted()
    }

    /// [from, to] 里说话重叠最多的人。
    func dominantSpeaker(from: Double, to: Double) -> Int? {
        var overlap: [Int: Double] = [:]
        for segment in segments() {
            let value = min(to, Double(segment.endTime)) - max(from, Double(segment.startTime))
            if value > 0 { overlap[segment.speakerIndex, default: 0] += value }
        }
        return overlap.max { $0.value < $1.value }?.key
    }

    /// 某个槽位的已定稿说话区间（分离器时间轴），新的在后。
    func finalizedSpeech(of speaker: Int) -> [ClosedRange<Double>] {
        guard let slot = diarizer.timeline.speakers[speaker] else { return [] }
        return slot.finalizedSegments.map { Double($0.startTime)...Double($0.endTime) }
    }

    /// 除「我」以外、已定稿说话时长（秒）。
    func finalizedSpeechSeconds() -> [Int: Double] {
        var out: [Int: Double] = [:]
        for (index, slot) in diarizer.timeline.speakers where index != userIndex {
            out[index] = Double(slot.finalizedSpeechDuration)
        }
        return out
    }

    /// 至今说话最多的人（没登记声纹时当作「我」：离麦克风最近、说得最多的通常是用户本人）。
    func mostTalkative() -> Int? {
        diarizer.timeline.speakers.values.max { $0.speechDuration < $1.speechDuration }?.index
    }

    /// 某人在 [from, to] 里的说话区间（含暂定结果）。
    func speech(of speaker: Int, from: Double, to: Double) -> [ClosedRange<Double>] {
        segments().compactMap { segment in
            guard segment.speakerIndex == speaker,
                  Double(segment.endTime) >= from, Double(segment.startTime) <= to else { return nil }
            return Double(segment.startTime)...Double(segment.endTime)
        }
    }

    /// 分离结果已经覆盖到哪一秒（定稿 + 暂定）。晚于它的时刻「还不知道是谁」。
    var analyzedUntil: Double {
        segments().map { Double($0.endTime) }.max() ?? 0
    }
}

// MARK: - 用户声纹

enum NativeVoiceprint {
    static let seconds: Double = 10
    static let minimumVoicedSeconds: Double = 5

    static var fileURL: URL {
        let base = (FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                    ?? FileManager.default.temporaryDirectory)
            .appendingPathComponent("BWReader/voiceprint", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent("user-16k.f32")
    }

    static var exists: Bool { FileManager.default.fileExists(atPath: fileURL.path) }

    static func load() -> [Float]? {
        guard let data = try? Data(contentsOf: fileURL), data.count >= 4 else { return nil }
        return data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
    }

    static func delete() { try? FileManager.default.removeItem(at: fileURL) }

    // MARK: 熟人声纹（用户 2026-09-26：「有可能为不同的人的声音建立特征然后标记名字么」→「直接做成声纹特征比对」）
    //
    // 存的是原声样本（≤30 秒）；声纹特征向量由 NativeSpeakerEmbedder 按需算、按 updatedAt 缓存 ——
    // 换更好的嵌入模型时不用让用户重新起名。

    struct Person: Codable, Identifiable, Equatable {
        let id: String
        var name: String
        var seconds: Double
        var updatedAt: Date
    }

    private static var peopleFolder: URL {
        let folder = fileURL.deletingLastPathComponent().appendingPathComponent("people", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }

    private static var peopleIndex: URL { peopleFolder.appendingPathComponent("people.json") }

    /// 最近更新的在前。
    static func people() -> [Person] {
        guard let data = try? Data(contentsOf: peopleIndex),
              let list = try? JSONDecoder().decode([Person].self, from: data) else { return [] }
        return list.sorted { $0.updatedAt > $1.updatedAt }
    }

    static func loadPerson(_ id: String) -> [Float]? {
        guard let data = try? Data(contentsOf: peopleFolder.appendingPathComponent(id + ".f32")), data.count >= 4 else { return nil }
        return data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
    }

    /// 同名就追加样本（最多留 30 秒），否则新建。返回保存后的有声秒数。
    @discardableResult
    static func savePerson(name: String, samples: [Float]) throws -> Double {
        var list = people()
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let existing = list.firstIndex { $0.name == trimmed }
        let id = existing.map { list[$0].id } ?? UUID().uuidString
        var merged = (existing != nil ? loadPerson(id) ?? [] : []) + samples
        let cap = Int(30 * NativeStreamDiarizer.sampleRate)
        if merged.count > cap { merged.removeFirst(merged.count - cap) }
        try merged.withUnsafeBufferPointer { Data(buffer: $0) }
            .write(to: peopleFolder.appendingPathComponent(id + ".f32"), options: .atomic)
        let seconds = Double(merged.count) / NativeStreamDiarizer.sampleRate
        let person = Person(id: id, name: trimmed, seconds: seconds, updatedAt: Date())
        if let existing { list[existing] = person } else { list.append(person) }
        try JSONEncoder().encode(list).write(to: peopleIndex, options: .atomic)
        return seconds
    }

    static func deletePerson(_ id: String) {
        var list = people()
        list.removeAll { $0.id == id }
        try? FileManager.default.removeItem(at: peopleFolder.appendingPathComponent(id + ".f32"))
        if let data = try? JSONEncoder().encode(list) { try? data.write(to: peopleIndex, options: .atomic) }
    }

    /// 只留有声的 20 ms 帧（静音会把声纹冲淡）。
    static func voicedOnly(_ samples: [Float]) -> [Float] {
        let frame = 320
        var out: [Float] = []
        var index = 0
        while index + frame <= samples.count {
            let slice = samples[index..<(index + frame)]
            let rms = sqrt(slice.reduce(0) { $0 + $1 * $1 } / Float(frame))
            if rms > 0.01 { out.append(contentsOf: slice) }
            index += frame
        }
        return out
    }

    /// 录 `seconds` 秒（调用方先停掉别的录音）。返回有声时长。
    static func record() async throws -> Double {
        let engine = AVAudioEngine()
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetoothHFP])
        try session.setActive(true)
        defer { try? session.setActive(false, options: .notifyOthersOnDeactivation) }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        final class Sink: @unchecked Sendable {
            let lock = NSLock()
            var resampler: NativeResampler16k
            var samples: [Float] = []
            init(rate: Double) { resampler = NativeResampler16k(sourceRate: rate) }
        }
        let sink = Sink(rate: format.sampleRate)
        input.installTap(onBus: 0, bufferSize: 4_096, format: format) { buffer, _ in
            guard let channel = buffer.floatChannelData?[0] else { return }
            let chunk = Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
            sink.lock.lock()
            sink.samples.append(contentsOf: sink.resampler.process(chunk))
            sink.lock.unlock()
        }
        engine.prepare()
        try engine.start()
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        input.removeTap(onBus: 0)
        engine.stop()
        sink.lock.lock()
        let voiced = voicedOnly(sink.samples)
        sink.lock.unlock()
        let voicedSeconds = Double(voiced.count) / NativeStreamDiarizer.sampleRate
        guard voicedSeconds >= minimumVoicedSeconds else {
            NativeAmbientLog.note(String(format: "声纹登记：有声只有 %.1f 秒，不够 %.0f 秒，未保存", voicedSeconds,
                                         minimumVoicedSeconds), level: "error")
            return voicedSeconds
        }
        let data = voiced.withUnsafeBufferPointer { Data(buffer: $0) }
        try data.write(to: fileURL, options: .atomic)
        NativeAmbientLog.note(String(format: "声纹登记：已保存（有声 %.1f 秒）", voicedSeconds))
        return voicedSeconds
    }
}

// MARK: - 重采样到 16 kHz

/// 整数倍（48k→16k 等）用盒式平均抽取（顺带低通，免得高频混叠进来）；其余用线性插值。
struct NativeResampler16k {
    let sourceRate: Double
    private let factor: Int?
    private var carry: [Float] = []
    private var position: Double = 0

    init(sourceRate: Double) {
        self.sourceRate = sourceRate
        let ratio = sourceRate / NativeStreamDiarizer.sampleRate
        factor = abs(ratio - ratio.rounded()) < 0.0001 && ratio >= 1 ? Int(ratio.rounded()) : nil
    }

    mutating func process(_ input: [Float]) -> [Float] {
        if let factor {
            if factor == 1 { return input }
            carry.append(contentsOf: input)
            let count = carry.count / factor
            var out = [Float](repeating: 0, count: count)
            for index in 0..<count {
                var sum: Float = 0
                for offset in 0..<factor { sum += carry[index * factor + offset] }
                out[index] = sum / Float(factor)
            }
            carry.removeFirst(count * factor)
            return out
        }
        let step = sourceRate / NativeStreamDiarizer.sampleRate
        var out: [Float] = []
        out.reserveCapacity(Int(Double(input.count) / step) + 1)
        while position < Double(input.count) {
            let left = Int(position)
            let fraction = Float(position - Double(left))
            let a = input[left]
            let b = left + 1 < input.count ? input[left + 1] : a
            out.append(a + (b - a) * fraction)
            position += step
        }
        position -= Double(input.count)
        return out
    }

    /// 48 kHz Int16 通话帧 → 16 kHz Float。
    static func from48kPCM(_ frame: [Int16]) -> [Float] {
        let count = frame.count / 3
        var out = [Float](repeating: 0, count: count)
        for index in 0..<count {
            let sum = Int32(frame[index * 3]) + Int32(frame[index * 3 + 1]) + Int32(frame[index * 3 + 2])
            out[index] = Float(sum) / 3 / 32_768
        }
        return out
    }
}
