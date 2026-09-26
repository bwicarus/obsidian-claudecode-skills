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

    static func patch(_ path: String, body: [String: Any], timeout: TimeInterval = 20) async throws -> [String: Any] {
        var request = try authorizedRequest(path, timeout: timeout)
        request.httpMethod = "PATCH"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return try await send(request)
    }

    static func delete(_ path: String, timeout: TimeInterval = 20) async throws -> [String: Any] {
        var request = try authorizedRequest(path, timeout: timeout)
        request.httpMethod = "DELETE"
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
            let body = (object?["code"] as? String) ?? String(decoding: data.prefix(160), as: UTF8.self)
            let server = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Server") ?? "?"
            // 带上路径与 Server 头：403 是 Flask、桥还是 tailscale serve 给的，一眼能分
            throw Failure.http(code, "\(request.url?.path ?? "?") server=\(server) \(body)")
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
        /// 服务器上的人物编号（KJ 节点 id，或 "me"）；只有本机样本、服务器还不认识时为 nil。
        var personId: String? = nil
    }

    /// 服务器声纹库（/api/ambient/voiceprints）：每人全部向量。5 分钟刷新一次；编辑人物后立即作废。
    private var serverPeople: [(personId: String, name: String, vectors: [[Float]], language: String?)] = []
    private var serverFetchedAt = Date.distantPast
    private var loggedServerFailure = false

    func invalidateServer() { serverFetchedAt = .distantPast }

    /// 这个人登记的语言（人物页里设置，存在服务器）；没登记返回 nil → 由转写器推测。
    func language(of personId: String) async -> String? {
        await refreshServerIfStale()
        return serverPeople.first { $0.personId == personId }?.language
    }

    private func refreshServerIfStale() async {
        guard Date().timeIntervalSince(serverFetchedAt) > 300 else { return }
        serverFetchedAt = Date()
        do {
            let reply = try await NativeAmbientServer.get("api/ambient/voiceprints")
            let rows = reply["people"] as? [[String: Any]] ?? []
            serverPeople = rows.compactMap { row -> (personId: String, name: String, vectors: [[Float]], language: String?)? in
                guard let id = row["personId"] as? String, let name = row["name"] as? String else { return nil }
                let vectors = (row["vectors"] as? [[Any]] ?? []).map { $0.compactMap { ($0 as? NSNumber)?.floatValue } }
                let language = (row["language"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                return (personId: id, name: name, vectors: vectors.filter { !$0.isEmpty }, language: language)
            }
            loggedServerFailure = false
        } catch {
            if !loggedServerFailure {
                loggedServerFailure = true
                NativeAmbientLog.note("声纹比对：取服务器声纹库失败，只用本机样本（\(error.localizedDescription)）", level: "error")
            }
        }
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

    /// 和「我」、本机熟人样本、服务器声纹库里的全部人比（同一个人多个来源取最近的那个）。
    func identify(_ vector: [Float]) async -> Match {
        await refreshServerIfStale()
        var scored: [(name: String, distance: Float, personId: String?)] = []
        if let user = await userVector() { scored.append(("我", Self.distance(vector, user), "me")) }
        for person in NativeVoiceprint.people() {
            guard let reference = await personVector(person) else { continue }
            let server = serverPeople.first { $0.name == person.name }?.personId
            scored.append((person.name, Self.distance(vector, reference), server))
        }
        for person in serverPeople {
            for reference in person.vectors where reference.count == vector.count {
                scored.append((person.name, Self.distance(vector, reference), person.personId))
            }
        }
        scored.sort { $0.distance < $1.distance }
        guard let best = scored.first else { return Match(name: nil, nearest: nil, distance: 2, runnerUp: 2) }
        // 同名（同一人多个来源）不算次近
        let runnerUp = scored.first { $0.name != best.name }?.distance ?? 2
        let accepted = best.distance < Self.matchDistance && runnerUp - best.distance >= Self.ambiguityMargin
        let personId = best.personId ?? scored.first { $0.name == best.name && $0.personId != nil }?.personId
        return Match(name: accepted ? best.name : nil, nearest: best.name, distance: best.distance, runnerUp: runnerUp,
                     personId: personId)
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
    /// 分离器在自己的队列上跑（模型慢时不能堵住识别结果与周期任务），查询在 work 队列上 —— 一把锁隔开。
    private let lock = NSRecursiveLock()   // 查询之间互相调用（activeSpeakers → elapsed / segments）
    /// 已排队、还没处理的样本数（判断是否跟不上实时）。
    private var pendingStorage = 0
    var pendingSamples: Int { lock.lock(); defer { lock.unlock() }; return pendingStorage }
    /// 累计处理耗时 / 已处理音频时长（实时率 = 前者 / 后者）。
    private var busyStorage = 0.0
    var busySeconds: Double { lock.lock(); defer { lock.unlock() }; return busyStorage }
    func enqueued(_ count: Int) -> Int { lock.lock(); defer { lock.unlock() }; pendingStorage += count; return pendingStorage }
    func locked<T>(_ body: () throws -> T) rethrows -> T { lock.lock(); defer { lock.unlock() }; return try body() }
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
    var elapsed: Double { lock.lock(); defer { lock.unlock() }; return Double(fedSamples) / Self.sampleRate }

    /// 已定稿的时间线推进到哪一秒（分离器时间轴）。早于它的「谁在说」不会再变。
    private var finalizedUntilStorage = 0.0
    var finalizedUntil: Double { lock.lock(); defer { lock.unlock() }; return finalizedUntilStorage }

    /// 送 16 kHz 单声道；够一个分块时模型才会真正跑。
    @discardableResult
    func feed(_ samples: [Float]) throws -> DiarizerTimelineUpdate? {
        lock.lock(); defer { lock.unlock() }
        let started = CFAbsoluteTimeGetCurrent()
        defer { busyStorage += CFAbsoluteTimeGetCurrent() - started; pendingStorage = max(0, pendingStorage - samples.count) }
        fedSamples += samples.count
        let update = try diarizer.process(samples: samples, sourceSampleRate: nil)
        if let chunk = update?.chunkResult {
            let frames = chunk.startFrame + chunk.finalizedFrameCount
            finalizedUntilStorage = max(finalizedUntilStorage, Double(frames) * Double(NativeSortformerModels.config.frameDurationSeconds))
        }
        return update
    }

    /// 全部人已定稿的说话片段（分离器时间轴），按开始时间排序。
    func finalizedSegments() -> [(speaker: Int, start: Double, end: Double)] {
        lock.lock(); defer { lock.unlock() }
        return diarizer.timeline.speakers.values
            .flatMap { slot in slot.finalizedSegments.map { (speaker: slot.index, start: Double($0.startTime), end: Double($0.endTime)) } }
            .sorted { $0.start < $1.start }
    }

    private func segments() -> [DiarizerSegment] {
        lock.lock(); defer { lock.unlock() }
        return diarizer.timeline.speakers.values.flatMap { $0.finalizedSegments + $0.tentativeSegments }
    }

    /// 最近 `window` 秒里，说话累计超过 `minSpeech` 秒的说话人。
    func activeSpeakers(within window: Double, minSpeech: Double) -> [Int] {
        lock.lock(); defer { lock.unlock() }
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
        lock.lock(); defer { lock.unlock() }
        var overlap: [Int: Double] = [:]
        for segment in segments() {
            let value = min(to, Double(segment.endTime)) - max(from, Double(segment.startTime))
            if value > 0 { overlap[segment.speakerIndex, default: 0] += value }
        }
        return overlap.max { $0.value < $1.value }?.key
    }

    /// 某个槽位的已定稿说话区间（分离器时间轴），新的在后。
    func finalizedSpeech(of speaker: Int) -> [ClosedRange<Double>] {
        lock.lock(); defer { lock.unlock() }
        guard let slot = diarizer.timeline.speakers[speaker] else { return [] }
        return slot.finalizedSegments.map { Double($0.startTime)...Double($0.endTime) }
    }

    /// 除「我」以外、已定稿说话时长（秒）。
    func finalizedSpeechSeconds() -> [Int: Double] {
        lock.lock(); defer { lock.unlock() }
        var out: [Int: Double] = [:]
        for (index, slot) in diarizer.timeline.speakers where index != userIndex {
            out[index] = Double(slot.finalizedSpeechDuration)
        }
        return out
    }

    /// 至今说话最多的人（没登记声纹时当作「我」：离麦克风最近、说得最多的通常是用户本人）。
    func mostTalkative() -> Int? {
        lock.lock(); defer { lock.unlock() }
        return diarizer.timeline.speakers.values.max { $0.speechDuration < $1.speechDuration }?.index
    }

    /// 某人在 [from, to] 里的说话区间（含暂定结果）。
    func speech(of speaker: Int, from: Double, to: Double) -> [ClosedRange<Double>] {
        lock.lock(); defer { lock.unlock() }
        return segments().compactMap { segment in
            guard segment.speakerIndex == speaker,
                  Double(segment.endTime) >= from, Double(segment.startTime) <= to else { return nil }
            return Double(segment.startTime)...Double(segment.endTime)
        }
    }

    /// 分离结果已经覆盖到哪一秒（定稿 + 暂定）。晚于它的时刻「还不知道是谁」。
    var analyzedUntil: Double {
        lock.lock(); defer { lock.unlock() }
        return segments().map { Double($0.endTime) }.max() ?? 0
    }
}

// MARK: - 送识别器前的自动增益

/// 远处的声音（隔着房间的电影、别人说话）进麦克风很小：分离器照样认得出有人在说，
/// 系统听写却判「没有语音」（1110），主线一句都出不来（2026-09-27 实测：6 分钟 0 句、重转 8 段全空）。
/// 送识别器前按平滑后的音量把它拉到正常说话的电平；峰值用 tanh 软限幅，不削波。只影响送识别器的那一份。
final class NativeAmbientGain {
    static let targetRMS: Float = 0.05          // ≈ -26 dBFS：近讲说话的典型电平
    static let maxGain: Float = 16
    private let lock = NSLock()
    private var smoothedRMS: Float = 0.05
    private var gain: Float = 1
    // 诊断：本统计周期的电平与增益
    private var sumSquares: Double = 0, count = 0, peak: Float = 0, gainSum: Double = 0, buffers = 0

    /// 单声道、增益后的一份（原生采样率），给识别请求用。
    func process(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let source = buffer.floatChannelData?[0], buffer.frameLength > 0,
              let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: buffer.format.sampleRate, channels: 1, interleaved: false),
              let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: buffer.frameLength),
              let target = out.floatChannelData?[0] else { return nil }
        let n = Int(buffer.frameLength)
        var squares: Float = 0, localPeak: Float = 0
        for i in 0..<n { let v = source[i]; squares += v * v; localPeak = max(localPeak, abs(v)) }
        let rms = sqrt(squares / Float(n))
        lock.lock()
        smoothedRMS = smoothedRMS * 0.9 + rms * 0.1
        let wanted = min(Self.maxGain, max(1, Self.targetRMS / max(smoothedRMS, 1e-5)))
        gain = gain * 0.8 + wanted * 0.2
        let g = gain
        sumSquares += Double(squares); count += n; peak = max(peak, localPeak); gainSum += Double(g); buffers += 1
        lock.unlock()
        for i in 0..<n { target[i] = tanh(source[i] * g) }
        out.frameLength = buffer.frameLength
        return out
    }

    /// 本周期的平均电平 / 峰值（dBFS）与平均增益，读完清零。
    func drainStats() -> (rmsDB: Double, peakDB: Double, gain: Double)? {
        lock.lock(); defer { sumSquares = 0; count = 0; peak = 0; gainSum = 0; buffers = 0; lock.unlock() }
        guard count > 0, buffers > 0 else { return nil }
        let rms = sqrt(sumSquares / Double(count))
        return (20 * log10(max(rms, 1e-7)), 20 * log10(Double(max(peak, 1e-7))), gainSum / Double(buffers))
    }

    /// 一整段（逐段重转用）：按整段音量一次性拉到目标电平，tanh 软限幅。
    static func normalize(_ samples: [Float]) -> [Float] {
        guard !samples.isEmpty else { return samples }
        let rms = sqrt(samples.reduce(Float(0)) { $0 + $1 * $1 } / Float(samples.count))
        let g = min(maxGain, max(1, targetRMS / max(rms, 1e-5)))
        return g == 1 ? samples : samples.map { tanh($0 * g) }
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
