import NaturalLanguage
import SwiftUI
import Translation

// 对话时间轴与人物（2026-09-26 用户：「系统地查看多人说话时的声音区分，时间轴 + 不同的块表示不同的发音者，
// 随时点块查看登记的资料（姓名 / 自定义介绍 / AI 整理 / 对话历史），都能直接编辑；
// 不同的块设成同一个名字就当作一个人」）。
//
// 数据全在服务器（/api/ambient/timeline·people·slots，格式见 references/ambient-people-format.md），
// 文字资料在 KJ 人物节点里（Obsidian KJ/）。这里只是一个查看与编辑的界面 —— 以后的独立 App 读写同一份。

// MARK: - 数据

struct NativeAmbientUtterance: Identifiable, Hashable {
    let id: String
    let t0: Double
    let t1: Double
    let windowId: String
    let slotKey: String
    let isUser: Bool
    let label: String
    let text: String
    let personId: String?
    let name: String?
    let lang: String
    let langConfirmed: Bool

    init?(_ row: [String: Any]) {
        guard let id = row["id"] as? String, let t0 = (row["t0"] as? NSNumber)?.doubleValue else { return nil }
        lang = row["lang"] as? String ?? ""
        langConfirmed = row["langConfirmed"] as? Bool ?? true
        self.id = id
        self.t0 = t0
        self.t1 = max(t0 + 300, (row["t1"] as? NSNumber)?.doubleValue ?? t0)
        windowId = row["windowId"] as? String ?? ""
        slotKey = row["slotKey"] as? String ?? ""
        isUser = row["isUser"] as? Bool ?? false
        label = row["label"] as? String ?? "?"
        text = row["text"] as? String ?? ""
        personId = row["personId"] as? String
        name = row["name"] as? String
    }

    /// 泳道：定了人按人（同名的块合成一条），没定人按声音块。
    var laneKey: String { personId.map { "p:" + $0 } ?? "s:" + slotKey }
    /// 名字 → 记录时的标签 → 由声音块编号推出的「说话人N」（重转补记的句子没有标签）。
    var displayName: String {
        if let name, !name.isEmpty { return name }
        if !label.isEmpty { return label }
        if let index = slotKey.split(separator: ":").last.flatMap({ Int($0) }) { return "说话人\(index + 1)" }
        return "?"
    }
}

struct NativeAmbientPersonInfo: Identifiable, Hashable {
    let id: String
    var name: String
    var intro: String
    var profile: String
    let isUser: Bool
    let aliases: [String]
    let voiceprints: Int
    let slots: Int
    var language: String
    let languageGuess: String
    let languageVotes: [String: Int]

    init?(_ row: [String: Any]) {
        guard let id = row["id"] as? String else { return nil }
        language = row["language"] as? String ?? ""
        languageGuess = row["languageGuess"] as? String ?? ""
        languageVotes = row["languageVotes"] as? [String: Int] ?? [:]
        self.id = id
        name = row["name"] as? String ?? ""
        intro = row["intro"] as? String ?? ""
        profile = row["profile"] as? String ?? ""
        isUser = row["isUser"] as? Bool ?? false
        aliases = row["aliases"] as? [String] ?? []
        voiceprints = row["voiceprints"] as? Int ?? 0
        slots = (row["slots"] as? [Any])?.count ?? 0
    }
}

enum NativeAmbientPalette {
    static let colors: [Color] = [.blue, .orange, .green, .pink, .purple, .teal, .brown, .indigo, .mint, .red]

    static func color(for key: String) -> Color {
        if key == "p:me" { return .accentColor }
        var hash: UInt32 = 2_166_136_261
        for byte in key.utf8 { hash = (hash ^ UInt32(byte)) &* 16_777_619 }
        return colors[Int(hash % UInt32(colors.count))]
    }
}

/// 时间轴译文的本机记录（2026-09-27 用户）：本机翻译记在本地；AI 精翻覆盖同一条；
/// 原文（这一块的句子）被删 / 被替换时绑定一起删。按块的第一句 id 记，带上整块的句子 id 与原文。
@MainActor
final class NativeAmbientTranslationStore {
    static let shared = NativeAmbientTranslationStore()
    struct Record: Codable {
        var ids: [String]          // 这一块全部句子的 id（绑定：任何一句没了 = 原文变了 / 被删了）
        var t0: Double
        var t1: Double
        var sourceText: String
        var text: String
        var source: String         // "apple" | "ai"
        var updatedAt: Double
    }
    private(set) var records: [String: Record] = [:]
    private let url: URL = {
        let base = (FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                    ?? FileManager.default.temporaryDirectory).appendingPathComponent("BWReader", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent("ambient-translations.json")
    }()

    private init() {
        if let data = try? Data(contentsOf: url), let decoded = try? JSONDecoder().decode([String: Record].self, from: data) {
            records = decoded
        }
    }

    /// 这一块当前有效的译文（原文没变才算）。
    func translation(for block: NativeAmbientTimelineModel.Block) -> Record? {
        guard let record = records[block.first.id], record.sourceText == block.text else { return nil }
        return record
    }

    /// 记一条。本机翻译不覆盖同一原文的 AI 精翻；AI 精翻一律覆盖。
    func put(_ block: NativeAmbientTimelineModel.Block, text: String, source: String) {
        if source == "apple", let old = translation(for: block), old.source == "ai" { return }
        records[block.first.id] = Record(ids: block.lines.map(\.id), t0: block.t0, t1: block.t1, sourceText: block.text,
                                         text: text, source: source, updatedAt: Date().timeIntervalSince1970)
    }

    /// 绑定删除：已加载的时间范围 [from, to] 内，句子已不在服务器上的记录删掉（原文被删 / 被重转替换）。
    @discardableResult
    func prune(loadedFrom from: Double, to: Double, present: Set<String>) -> Int {
        let stale = records.filter { $0.value.t0 >= from && $0.value.t1 <= to && !$0.value.ids.allSatisfy(present.contains) }.map(\.key)
        for key in stale { records.removeValue(forKey: key) }
        if records.count > 20_000 {   // 容量上限：删最旧的
            for key in records.sorted(by: { $0.value.updatedAt < $1.value.updatedAt }).prefix(records.count - 20_000).map(\.key) {
                records.removeValue(forKey: key)
            }
        }
        return stale.count
    }

    func save() {
        if let data = try? JSONEncoder().encode(records) { try? data.write(to: url, options: .atomic) }
    }
}

@MainActor
final class NativeAmbientTimelineModel: ObservableObject {
    enum Span: String, CaseIterable, Identifiable {
        case hour = "1 小时", threeHours = "3 小时", today = "今天", yesterday = "昨天"
        var id: String { rawValue }
    }

    @Published var range: Span = .threeHours
    /// 显示范围的两端（绝对时刻，毫秒）。nil = 钉在该端：新端钉在「现在」、旧端钉在范围起点 ——
    /// 时间流逝范围变长时也还钉着（2026-09-27 用户：「左端拉到最左就锁定为最左，即使时间流逝导致时间条变长」）。
    @Published var newestEdge: Double?
    @Published var oldestEdge: Double?
    /// 「现在」：自动刷新时更新，范围随之往前走。
    @Published var now = Date()

    // 批量翻译（2026-09-27 用户：「加一个翻译按钮批量翻译，结果放在原句下面，默认用 Apple 的翻译」）
    @Published var translateOn = false
    @Published var storeRevision = 0                     // 本机译文记录有变化 → 重画
    @Published var translationVersion = 0                 // 变了 = 有新的待翻（翻译器据此开下一组）
    @Published var translationNote: String?
    @Published var refining = false
    /// 每一块的翻译进行状态（按块第一句 id）；完成 = 有译文记录，不在这里。
    enum RowState: Equatable { case queued(String), working(String), failed(String) }
    @Published var rowStates: [String: RowState] = [:]
    /// 顶部进度：当前这一轮翻译（本机或 AI）已完成 / 总数。
    struct Job: Equatable { var label: String; var done: Int; var total: Int; var finished = false }
    @Published var job: Job?
    private var appleGroupIDs: [String: [String]] = [:]   // 一组里的原文 → 对应块的 id
    private var translationInFlight: Set<String> = []
    private var translationFailed: Set<String> = []

    /// 下一组待翻的块（同一种原文语言一组，最多 60 句）；中文的不翻。
    func nextTranslationGroup() -> (language: String?, texts: [String])? {
        guard translateOn else { return nil }
        var byLanguage: [String: [String]] = [:], order: [String] = []
        for block in groups.flatMap(\.blocks) {
            let text = block.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty, NativeAmbientTranslationStore.shared.translation(for: block) == nil,
                  !translationInFlight.contains(text), !translationFailed.contains(text) else { continue }
            let detected = Self.sourceLanguage(of: text)
            if detected == .simplifiedChinese || detected == .traditionalChinese { continue }
            let key = detected?.rawValue ?? ""
            if byLanguage[key] == nil { order.append(key) }
            byLanguage[key, default: []].append(text)
        }
        // 所有待翻的块先标「等待」，进度总数随之更新（自动刷新进来的新句也算进来）
        var queued = 0
        for block in groups.flatMap(\.blocks) {
            let text = block.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard byLanguage.values.contains(where: { $0.contains(text) }) else { continue }
            if rowStates[block.first.id] == nil { rowStates[block.first.id] = .queued("本机"); queued += 1 }
        }
        if queued > 0 {
            if var current = job, current.label == "本机翻译", !current.finished { current.total += queued; job = current }
            else { job = Job(label: "本机翻译", done: 0, total: queued) }
        }
        guard let key = order.first, let texts = byLanguage[key]?.prefix(60) else {
            finishJob("本机翻译")
            return nil
        }
        translationInFlight.formUnion(texts)
        for block in groups.flatMap(\.blocks) where texts.contains(block.text.trimmingCharacters(in: .whitespacesAndNewlines)) {
            rowStates[block.first.id] = .working("本机")
            appleGroupIDs[block.text.trimmingCharacters(in: .whitespacesAndNewlines), default: []].append(block.first.id)
        }
        return (key.isEmpty ? nil : key, Array(texts))
    }

    /// 原文语言：只在常见语言里判（2026-09-27 实测：不限候选时「Right.」「Who I」这种短句会被判成冷门语言，
    /// 一句一组、全部 Unable to Translate）。判不出来返回 nil（交给翻译框架自己认）。
    static func sourceLanguage(of text: String) -> NLLanguage? {
        let recognizer = NLLanguageRecognizer()
        recognizer.languageConstraints = [.english, .japanese, .korean, .simplifiedChinese, .traditionalChinese,
                                          .french, .german, .spanish]
        recognizer.processString(text)
        return recognizer.dominantLanguage
    }

    func finishTranslation(_ texts: [String], results: [String: String], error: String?) {
        translationInFlight.subtract(texts)
        let store = NativeAmbientTranslationStore.shared
        for block in groups.flatMap(\.blocks) {
            if let value = results[block.text.trimmingCharacters(in: .whitespacesAndNewlines)] { store.put(block, text: value, source: "apple") }
        }
        if !results.isEmpty { store.save(); storeRevision += 1 }
        let missing = texts.filter { results[$0] == nil }
        var finishedBlocks = 0
        for text in texts {
            for id in appleGroupIDs.removeValue(forKey: text) ?? [] {
                finishedBlocks += 1
                rowStates[id] = results[text] == nil ? .failed("本机") : nil
            }
        }
        if var current = job, current.label == "本机翻译" { current.done += finishedBlocks; job = current }
        if let error {
            translationFailed.formUnion(missing)
            translationNote = "翻译失败：" + error
            NativeAmbientLog.note("时间轴：翻译失败（\(missing.count) 句，首句「\(missing.first?.prefix(40) ?? "")」）\(error)", level: "error")
        }
        translationVersion += 1   // 接着翻下一组
    }

    private func finishJob(_ label: String) {
        guard var current = job, current.label == label, !current.finished else { return }
        current.finished = true
        current.done = current.total
        job = current
        Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(3))
            if self?.job?.finished == true { self?.job = nil }
        }
    }

    /// 精翻（2026-09-27 用户）：只翻滑条选中范围内的对话 —— 按时间先后整理成「说话人：原文」一次交给 AI，
    /// 结合上下文（含出场人物的介绍）整体翻译、按顺序逐条给回。一次最多 200 块（取最新的）。
    func refineVisible() async {
        let blocks = Array(groups.reversed().flatMap { $0.blocks.reversed() }.suffix(200))
        guard !blocks.isEmpty, !refining else { return }
        refining = true
        translateOn = true
        translationNote = nil
        defer { refining = false }
        for block in blocks { rowStates[block.first.id] = .queued("AI") }
        job = Job(label: "AI 精翻", done: 0, total: blocks.count)
        let line = { (block: Block) -> [String: Any] in
            ["speaker": block.first.displayName, "text": block.text, "personId": block.first.personId ?? ""]
        }
        let store = NativeAmbientTranslationStore.shared
        var translated = 0, failedChunks = 0
        // 分批：每批 20 块、附前面 12 块当上下文 —— 进度能一批批往前走，也不丢前后文
        for start in stride(from: 0, to: blocks.count, by: 20) {
            let chunk = Array(blocks[start..<min(blocks.count, start + 20)])
            let context = blocks[max(0, start - 12)..<start].map(line)
            for block in chunk { rowStates[block.first.id] = .working("AI") }
            do {
                let reply = try await NativeAmbientServer.post("api/ambient/translate",
                                                               body: ["lines": chunk.map(line), "context": Array(context)], timeout: 300)
                let out = reply["translations"] as? [String] ?? []
                for (index, block) in chunk.enumerated() {
                    if index < out.count, !out[index].isEmpty {
                        store.put(block, text: out[index], source: "ai")   // AI 精翻覆盖本机翻译
                        rowStates[block.first.id] = nil
                        translated += 1
                    } else {
                        rowStates[block.first.id] = .failed("AI")
                    }
                }
                store.save()
                storeRevision += 1
            } catch {
                failedChunks += 1
                for block in chunk { rowStates[block.first.id] = .failed("AI") }
                translationNote = "精翻有一批失败：\(error.localizedDescription)"
                NativeAmbientLog.note("时间轴：精翻一批失败 \(error.localizedDescription)", level: "error")
            }
            if var current = job { current.done += chunk.count; job = current }
        }
        finishJob("AI 精翻")
        NativeAmbientLog.note("时间轴：精翻 \(translated)/\(blocks.count) 块" + (failedChunks > 0 ? "，\(failedChunks) 批失败" : ""))
    }

    /// 这一块显示的译文：精翻优先，其次 Apple 机翻；与原文相同（本来就是中文）不显示。
    func translation(for block: Block) -> NativeAmbientTranslationStore.Record? {
        guard translateOn, let record = NativeAmbientTranslationStore.shared.translation(for: block) else { return nil }
        let original = block.text.trimmingCharacters(in: .whitespacesAndNewlines)
        return record.text == original ? nil : record
    }

    func toggleTranslation() {
        translateOn.toggle()
        translationNote = nil
        if !translateOn {
            rowStates = rowStates.filter { if case .working = $0.value { return true }; return false }
            if job?.label == "本机翻译" { job = nil }
        }
        translationFailed = []
        translationVersion += 1
    }
    @Published private(set) var utterances: [NativeAmbientUtterance] = []
    @Published private(set) var people: [NativeAmbientPersonInfo] = []
    @Published private(set) var loading = false
    @Published var error: String?

    func bounds() -> (Double, Double) {
        let now = self.now
        let calendar = Calendar.current
        let startOfToday = calendar.startOfDay(for: now)
        let ms = { (date: Date) in date.timeIntervalSince1970 * 1000 }
        switch range {
        case .hour: return (ms(now) - 3_600_000, ms(now))
        case .threeHours: return (ms(now) - 10_800_000, ms(now))
        case .today: return (ms(startOfToday), ms(now))
        case .yesterday: return (ms(startOfToday) - 86_400_000, ms(startOfToday))
        }
    }

    func reload() async {
        now = Date()
        loading = true
        defer { loading = false }
        let (from, to) = bounds()
        do {
            let timeline = try await NativeAmbientServer.get("api/ambient/timeline?from=\(Int(from))&to=\(Int(to))")
            utterances = (timeline["utterances"] as? [[String: Any]] ?? []).compactMap(NativeAmbientUtterance.init)
            // 绑定删除：这段时间里原文已不在的译文记录一起删
            let removed = NativeAmbientTranslationStore.shared.prune(loadedFrom: from, to: to, present: Set(utterances.map(\.id)))
            if removed > 0 {
                NativeAmbientTranslationStore.shared.save()
                storeRevision += 1
                NativeAmbientLog.note("时间轴：原文已删，同时删掉 \(removed) 条译文记录")
            }
            if translateOn { translationVersion += 1 }   // 自动刷新进来的新句子也翻
            let list = try await NativeAmbientServer.get("api/ambient/people")
            people = (list["people"] as? [[String: Any]] ?? []).compactMap(NativeAmbientPersonInfo.init)
            error = nil
        } catch {
            self.error = "读取失败：\(error.localizedDescription)"
            NativeAmbientLog.note("时间轴：读取失败 \(error.localizedDescription)", level: "error")
        }
    }

    var startTime: Double {
        let (from, to) = bounds()
        return min(to, max(from, oldestEdge ?? from))
    }
    var endTime: Double {
        let (from, to) = bounds()
        return max(startTime, min(to, newestEdge ?? to))
    }

    /// 滑条用：左 = 现在（0），右 = 范围起点（1）。
    var newerFraction: Double {
        get { let (from, to) = bounds(); return (to - endTime) / max(1, to - from) }
        set {
            let (from, to) = bounds()
            newestEdge = newValue <= 0.004 ? nil : to - newValue * (to - from)
        }
    }
    var olderFraction: Double {
        get { let (from, to) = bounds(); return (to - startTime) / max(1, to - from) }
        set {
            let (from, to) = bounds()
            oldestEdge = newValue >= 0.996 ? nil : to - newValue * (to - from)
        }
    }

    /// 刻度间隔：让一段时长里大约有 4–8 个刻度。
    static func tickInterval(for span: Double) -> Double {
        let minutes: [Double] = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360]
        return (minutes.first { span / ($0 * 60_000) <= 8 } ?? 720) * 60_000
    }
    static func ticks(from: Double, to: Double) -> [Double] {
        guard to > from else { return [] }
        let step = tickInterval(for: to - from)
        let offset = Double(TimeZone.current.secondsFromGMT()) * 1000   // 刻度对齐本地整点 / 整分
        var t = (((from + offset) / step).rounded(.up)) * step - offset
        var out: [Double] = []
        while t <= to && out.count < 50 { out.append(t); t += step }
        return out
    }

    /// 一个人连续说的几句合成一块（2026-09-27 用户：「一个人连续说话完全可以结合为一个块」）。
    struct Block: Identifiable {
        let lines: [NativeAmbientUtterance]        // 时间先后
        var id: String { lines.first?.id ?? "" }
        var first: NativeAmbientUtterance { lines[0] }
        var t0: Double { lines.first?.t0 ?? 0 }
        var t1: Double { lines.map(\.t1).max() ?? 0 }
        var text: String {
            lines.map(\.text).reduce("") { joined, next in
                guard let last = joined.last, let head = next.first else { return joined + next }
                let cjk = { (c: Character) in c.unicodeScalars.contains { (0x3000...0x9FFF).contains($0.value) || (0xFF00...0xFFEF).contains($0.value) } }
                return joined + (cjk(last) || cjk(head) ? "" : " ") + next
            }
        }
    }

    /// 一段对话：中间停顿超过 60 秒另起一段。段、段里的块都是新的在上。
    struct Group: Identifiable {
        let blocks: [Block]
        var id: String { blocks.first?.id ?? "" }
        var start: Double { blocks.last?.t0 ?? 0 }
        var end: Double { blocks.first?.t1 ?? 0 }
        var sentences: Int { blocks.reduce(0) { $0 + $1.lines.count } }
        var speakers: Int { Set(blocks.map(\.first.laneKey)).count }
    }

    var groups: [Group] {
        let start = startTime, end = endTime
        let visible = utterances.filter { $0.t1 >= start && $0.t0 <= end }.sorted { $0.t0 < $1.t0 }
        var runs: [[NativeAmbientUtterance]] = []   // 先按时间先后切段
        for u in visible {
            if let last = runs.last?.last, u.t0 - last.t1 <= 60_000 { runs[runs.count - 1].append(u) }
            else { runs.append([u]) }
        }
        return runs.reversed().map { run in
            var blocks: [[NativeAmbientUtterance]] = []
            for u in run {
                if let last = blocks.last?.last, last.laneKey == u.laneKey { blocks[blocks.count - 1].append(u) }
                else { blocks.append([u]) }
            }
            return Group(blocks: blocks.reversed().map { Block(lines: $0) })
        }
    }

    func deletePerson(_ id: String, purge: Bool) async -> String? {
        do {
            let reply = try await NativeAmbientServer.delete("api/ambient/people/\(id)" + (purge ? "?purge=1" : ""))
            await NativeSpeakerEmbedder.shared.invalidateServer()
            NativeAmbientLog.note("人物：已删除 \(id)（声纹 \(reply["voiceprints"] ?? 0) 条、声音块 \(reply["slots"] ?? 0) 个"
                + (purge ? "、连同 \(reply["utterances"] ?? 0) 句话）" : "退回未定人）"))
            await reload()
            return nil
        } catch {
            NativeAmbientLog.note("人物：删除失败 \(error.localizedDescription)", level: "error")
            return error.localizedDescription
        }
    }

    func window(_ id: String) -> [NativeAmbientUtterance] {
        utterances.filter { $0.windowId == id }
    }

    func assign(slotKey: String, name: String) async -> String? {
        do {
            _ = try await NativeAmbientServer.post("api/ambient/slots/assign", body: ["slotKey": slotKey, "name": name])
            await NativeSpeakerEmbedder.shared.invalidateServer()
            NativeAmbientLog.note("时间轴：声音块 \(slotKey) 定为「\(name)」")
            await reload()
            return nil
        } catch {
            NativeAmbientLog.note("时间轴：定人失败 \(error.localizedDescription)", level: "error")
            return error.localizedDescription
        }
    }

    /// 还没定人的「说话人N」：删掉这个声音块的全部话。
    func deleteSlot(_ slotKey: String) async -> String? {
        do {
            let reply = try await NativeAmbientServer.post("api/ambient/slots/delete", body: ["slotKey": slotKey])
            NativeAmbientLog.note("时间轴：已删除声音块 \(slotKey) 的 \(reply["utterances"] ?? 0) 句话")
            await reload()
            return nil
        } catch {
            NativeAmbientLog.note("时间轴：删除声音块失败 \(error.localizedDescription)", level: "error")
            return error.localizedDescription
        }
    }

    func unassign(slotKey: String) async {
        do {
            _ = try await NativeAmbientServer.post("api/ambient/slots/unassign", body: ["slotKey": slotKey])
            await NativeSpeakerEmbedder.shared.invalidateServer()
            await reload()
        } catch {
            self.error = "取消失败：\(error.localizedDescription)"
        }
    }
}

// MARK: - 时间轴页面

struct NativeAmbientTimelineView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var model = NativeAmbientTimelineModel()
    @State private var selected: NativeAmbientUtterance?
    @State private var deleting: NativeAmbientPersonInfo?

    var body: some View {
        NavigationStack {
            List {
                controls
                if let error = model.error { Section { Text(error).foregroundStyle(.red) } }
                if let note = model.translationNote { Section { Text(note).font(.caption).foregroundStyle(.secondary) } }
                timeline
                peopleSection
            }
            .navigationTitle("对话时间轴与人物")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .topBarLeading) {
                    Button("刷新", systemImage: "arrow.clockwise") { Task { await model.reload() } }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("AI 精翻", systemImage: "sparkles") { Task { await model.refineVisible() } }
                        .labelStyle(.titleAndIcon)
                        .disabled(model.refining || model.groups.isEmpty)
                }
                ToolbarItem(placement: .primaryAction) {
                    Button(model.translateOn ? "隐藏译文" : "本机翻译", systemImage: "translate") {
                        if #available(iOS 18.0, *) { model.toggleTranslation() }
                        else { model.translationNote = "批量翻译需要 iOS 18 以上" }
                    }
                    .labelStyle(.titleAndIcon)
                }
            }
            .background {
                if #available(iOS 18.0, *) { NativeAmbientTranslator(model: model) }
            }
            .task { await model.reload() }
            .onChange(of: model.range) { _, _ in model.newestEdge = nil; model.oldestEdge = nil; Task { await model.reload() } }
            .task {
                // 自动刷新（2026-09-27 用户：「刷新是自动的」）：每 10 秒拉一次，「现在」跟着往前走
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(10))
                    guard !Task.isCancelled else { return }
                    await model.reload()
                }
            }
            .confirmationDialog("删除人物", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                                presenting: deleting) { person in
                Button("删除「\(person.name)」和他说过的全部话", role: .destructive) {
                    Task { if let failure = await model.deletePerson(person.id, purge: true) { model.error = "删除失败：\(failure)" } }
                }
                Button("只删人物（他的话留在时间轴上）", role: .destructive) {
                    Task { if let failure = await model.deletePerson(person.id, purge: false) { model.error = "删除失败：\(failure)" } }
                }
            } message: { person in
                Text("都会删掉他的声纹、从人物列表里去掉。KJ 人物页（Obsidian）保留，要彻底删在 Obsidian 里删那一页。")
            }
            .sheet(item: $selected) { utterance in
                NativeAmbientBlockSheet(utterance: utterance, model: model)
            }
        }
    }

    private var controls: some View {
        Section {
            if let job = model.job {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(job.finished ? job.label + "完成" : job.label + "中").font(.subheadline)
                        Spacer()
                        Text("\(job.done) / \(job.total)").font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                    }
                    ProgressView(value: Double(min(job.done, job.total)), total: Double(max(1, job.total)))
                }
            }
            Picker("范围", selection: $model.range) {
                ForEach(NativeAmbientTimelineModel.Span.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            let (from, to) = model.bounds()
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("显示").font(.subheadline)
                    Spacer()
                    Text((model.newestEdge == nil ? "现在" : Self.clock(model.endTime)) + " ← " + Self.clock(model.startTime))
                        .font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                }
                NativeAmbientRangeSlider(newer: $model.newerFraction, older: $model.olderFraction, from: from, to: to)
            }
        } footer: {
            Text("「本机翻译」用 Apple 翻译在 iPad 上离线翻；「AI 精翻」只翻滑条选中的范围（最多 200 块），交给服务器上的 AI 结合上下文与人物介绍整体翻译。"
                 + "左边是现在，往右越早。拖两端的圆点调显示范围；左端拉到最左就一直跟着现在。每 10 秒自动刷新。下面新的在上，一个人连着说的合成一块；点一块查看、定人或删除。")
        }
    }

    static func clock(_ ms: Double) -> String {
        Date(timeIntervalSince1970: ms / 1000).formatted(date: .omitted, time: .shortened)
    }

    @ViewBuilder private var timeline: some View {
        if model.loading && model.utterances.isEmpty {
            Section { ProgressView("读取中…") }
        } else if model.groups.isEmpty {
            Section { Text("这段时间没有旁听记录。").foregroundStyle(.secondary) }
        } else {
            ForEach(model.groups) { group in
                Section {
                    ForEach(Array(group.blocks.enumerated()), id: \.element.id) { index, block in
                        // 时间刻度：这一块与上面（更新的）一块之间跨过的整点刻度，画成一条刻度线
                        let newer = index > 0 ? group.blocks[index - 1].t0 : group.end
                        ForEach(NativeAmbientTimelineModel.ticks(from: block.t1, to: newer).reversed(), id: \.self) { tick in
                            NativeAmbientTickRow(time: tick)
                        }
                        NativeAmbientTimelineRow(block: block, translation: model.translation(for: block),
                                                 state: model.rowStates[block.first.id])
                            .contentShape(Rectangle())
                            .onTapGesture { selected = block.first }
                    }
                } header: {
                    let start = Date(timeIntervalSince1970: group.start / 1000)
                    Text("\(start.formatted(date: .abbreviated, time: .shortened)) – \(Self.clock(group.end))"
                         + " · \(group.sentences) 句 · \(group.speakers) 人")
                }
            }
        }
    }

    private var peopleSection: some View {
        Section("人物") {
            ForEach(model.people) { person in
                NavigationLink {
                    NativeAmbientPersonView(personId: person.id, onChange: { Task { await model.reload() } })
                } label: {
                    HStack {
                        Circle().fill(NativeAmbientPalette.color(for: "p:" + person.id)).frame(width: 10, height: 10)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(person.name)
                            if !person.intro.isEmpty {
                                Text(person.intro).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            }
                        }
                        Spacer()
                        Text("\(person.slots) 块 · \(person.voiceprints) 声纹").font(.caption2).foregroundStyle(.secondary)
                    }
                }
                .swipeActions(edge: .trailing) {
                    if !person.isUser {
                        Button("删除", role: .destructive) { deleting = person }
                    }
                }
            }
        }
    }
}

// MARK: - 画布

/// 时间轴的一块：一个人连着说的几句 —— 起止时间 · 说话人 · 全文（长句换行显示完整）。
struct NativeAmbientTimelineRow: View {
    let block: NativeAmbientTimelineModel.Block
    var translation: NativeAmbientTranslationStore.Record?
    var state: NativeAmbientTimelineModel.RowState?

    var body: some View {
        let first = block.first
        let color = NativeAmbientPalette.color(for: first.laneKey)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(Self.time(block.t0)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                if block.t1 - block.t0 >= 5_000 {
                    Text(Self.time(block.t1)).font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
                }
            }
            .frame(width: 62, alignment: .leading)
            Text(first.displayName).font(.caption.bold()).foregroundStyle(color).lineLimit(2)
                .frame(width: 70, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(block.text).font(.callout).fixedSize(horizontal: false, vertical: true)
                if let translation, !translation.text.isEmpty {
                    (Text(translation.text) + Text(translation.source == "ai" ? "  · AI" : "  · 本机").font(.caption2).foregroundColor(.gray))
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                switch state {
                case .queued(let who):
                    Text(who == "AI" ? "等待 AI 精翻…" : "等待本机翻译…").font(.caption).foregroundStyle(.tertiary)
                case .working(let who):
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text(who == "AI" ? "AI 精翻中…" : "本机翻译中…").font(.caption).foregroundStyle(.secondary)
                    }
                case .failed(let who):
                    Text(who == "AI" ? "AI 精翻失败" : "本机翻译失败").font(.caption).foregroundStyle(.red)
                case nil:
                    EmptyView()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, 6)
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 1.5).fill(color.opacity(first.personId == nil ? 0.45 : 0.9)).frame(width: 3)
        }
    }

    static func time(_ ms: Double) -> String {
        Date(timeIntervalSince1970: ms / 1000).formatted(date: .omitted, time: .standard)
    }
}

/// 句子时间轴上的刻度线（整点 / 整分，间隔随显示范围变）。
struct NativeAmbientTickRow: View {
    let time: Double

    var body: some View {
        HStack(spacing: 6) {
            Text(NativeAmbientTimelineView.clock(time)).font(.caption2.monospacedDigit().bold()).foregroundStyle(.secondary)
            Rectangle().fill(Color.secondary.opacity(0.3)).frame(height: 1)
        }
        .listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 2, leading: 16, bottom: 2, trailing: 16))
    }
}

/// 两端可拖的时间范围滑条，带时间刻度。左 = 现在（0），右 = 范围起点（1）。
/// 整条只有一个拖动手势：按下时离哪个圆点近就拖哪个（两个圆点各挂手势时，offset 后的命中区域还在原地，
/// 拖一个会带动另一个 —— 2026-09-27 模拟器实测）。
struct NativeAmbientRangeSlider: View {
    @Binding var newer: Double
    @Binding var older: Double
    let from: Double
    let to: Double
    private let knob: CGFloat = 26
    private let minGap = 0.01
    @State private var draggingNewer: Bool?

    var body: some View {
        GeometryReader { geo in
            let width = max(1, geo.size.width - knob)
            let x = { (fraction: Double) in knob / 2 + CGFloat(fraction) * width }
            let fraction = { (location: CGFloat) in min(1, max(0, Double((location - knob / 2) / width))) }
            ZStack(alignment: .topLeading) {
                Capsule().fill(Color.secondary.opacity(0.25))
                    .frame(width: width, height: 4).offset(x: knob / 2, y: knob / 2 - 2)
                Capsule().fill(Color.accentColor)
                    .frame(width: max(0, x(older) - x(newer)), height: 4).offset(x: x(newer), y: knob / 2 - 2)
                ForEach(NativeAmbientTimelineModel.ticks(from: from, to: to), id: \.self) { tick in
                    let at = (to - tick) / max(1, to - from)
                    Rectangle().fill(Color.secondary.opacity(0.6)).frame(width: 1, height: 6)
                        .offset(x: x(at), y: knob / 2 + 6)
                    Text(NativeAmbientTimelineView.clock(tick)).font(.system(size: 9).monospacedDigit())
                        .foregroundStyle(.secondary).fixedSize()
                        .frame(width: 44).offset(x: x(at) - 22, y: knob / 2 + 13)
                }
                handle(pinned: newer <= 0.004).offset(x: x(newer) - knob / 2)
                handle(pinned: older >= 0.996).offset(x: x(older) - knob / 2)
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 1).onChanged { value in
                let target = fraction(value.location.x)
                if draggingNewer == nil {
                    let start = fraction(value.startLocation.x)
                    draggingNewer = abs(start - newer) <= abs(start - older)
                }
                if draggingNewer == true { newer = min(target, older - minGap) }
                else { older = max(target, newer + minGap) }
            }.onEnded { _ in draggingNewer = nil })
        }
        .frame(height: 52)
    }

    /// 钉住的一端（最左 = 跟着现在 / 最右 = 范围起点）填满颜色，一眼看出它会跟着走。
    private func handle(pinned: Bool) -> some View {
        Circle().fill(pinned ? Color.accentColor : Color.white).frame(width: knob, height: knob)
            .shadow(color: .black.opacity(0.25), radius: 2, y: 1)
            .overlay(Circle().stroke(Color.accentColor, lineWidth: 1.5))
            .allowsHitTesting(false)
    }
}

/// Apple 翻译（Translation 框架，本机翻译）：按原文语言分组，一组一批翻成简体中文。
/// 语言包没装时系统会弹窗让用户下载。
@available(iOS 18.0, *)
struct NativeAmbientTranslator: View {
    @ObservedObject var model: NativeAmbientTimelineModel
    @State private var config: TranslationSession.Configuration?
    @State private var group: [String] = []
    @State private var busy = false

    var body: some View {
        Color.clear
            .onChange(of: model.translationVersion) { _, _ in startNext() }
            .translationTask(config) { session in
                let texts = group
                var results: [String: String] = [:]
                var failure: String?
                let pair = "\(config?.source?.minimalIdentifier ?? "自动")→\(config?.target?.minimalIdentifier ?? "?")"
                do {
                    try await session.prepareTranslation()   // 语言包没装：系统弹窗让用户下载
                    let requests = texts.map { TranslationSession.Request(sourceText: $0, clientIdentifier: $0) }
                    for try await response in session.translate(batch: requests) {
                        if let key = response.clientIdentifier { results[key] = response.targetText }
                    }
                } catch {
                    failure = "\(pair) \(String(describing: error))"
                }
                await MainActor.run {
                    busy = false
                    model.finishTranslation(texts, results: results, error: failure)
                }
            }
    }

    private func startNext() {
        guard !busy, let next = model.nextTranslationGroup() else { return }
        busy = true
        group = next.texts
        let source = next.language.map { Locale.Language(identifier: $0) }
        let target = Locale.Language(identifier: "zh-Hans")
        if let source {
            // 先问系统这对语言支不支持：不支持的整组直接出声跳过，不必进翻译框架再失败
            Task { @MainActor in
                let status = await LanguageAvailability().status(from: source, to: target)
                if status == .unsupported {
                    busy = false
                    model.finishTranslation(next.texts, results: [:],
                                            error: "Apple 翻译不支持 \(source.minimalIdentifier)→简体中文")
                    return
                }
                run(source: source, target: target)
            }
            return
        }
        run(source: nil, target: target)
    }

    private func run(source: Locale.Language?, target: Locale.Language) {
        if var current = config, current.source == source, current.target == target {
            current.invalidate()     // 同一对语言：让 translationTask 再跑一次
            config = current
        } else {
            config = TranslationSession.Configuration(source: source, target: target)
        }
    }
}

// MARK: - 点块

struct NativeAmbientBlockSheet: View {
    let utterance: NativeAmbientUtterance
    @ObservedObject var model: NativeAmbientTimelineModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var picked = ""
    @State private var note = ""
    @State private var busy = false
    @State private var confirmDeleteSlot = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(utterance.text).textSelection(.enabled)
                    LabeledContent("说话人", value: utterance.displayName)
                    if !utterance.lang.isEmpty {
                        LabeledContent("语言", value: NativeSegmentTranscriber.displayName(utterance.lang)
                            + (utterance.langConfirmed ? "" : "（推测）"))
                    }
                    LabeledContent("时间", value: Date(timeIntervalSince1970: utterance.t0 / 1000)
                        .formatted(date: .abbreviated, time: .standard))
                }
                whoSection
                Section("当时的对话（全部人）") {
                    ForEach(model.window(utterance.windowId)) { line in
                        HStack(alignment: .top, spacing: 6) {
                            Text(line.displayName)
                                .font(.caption.bold())
                                .foregroundStyle(NativeAmbientPalette.color(for: line.laneKey))
                                .frame(width: 64, alignment: .leading)
                            Text(line.text).font(.callout).textSelection(.enabled)
                        }
                        .listRowBackground(line.id == utterance.id ? Color.accentColor.opacity(0.12) : nil)
                    }
                }
            }
            .navigationTitle(utterance.displayName)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .confirmationDialog("删除", isPresented: $confirmDeleteSlot) {
                Button("删除这个声音块的全部话", role: .destructive) {
                    busy = true
                    Task {
                        if let failure = await model.deleteSlot(utterance.slotKey) { note = failure } else { dismiss() }
                        busy = false
                    }
                }
            } message: { Text("时间轴上「\(utterance.displayName)」（这个声音块）的每一句都会删掉。") }
        }
    }

    @ViewBuilder private var whoSection: some View {
        if let personId = utterance.personId {
            Section("这个人") {
                NavigationLink("查看 / 编辑「\(utterance.displayName)」的资料") {
                    NativeAmbientPersonView(personId: personId, onChange: { Task { await model.reload() } })
                }
                if !utterance.slotKey.isEmpty && personId != "me" {
                    Button("这一块不是他（取消定人）", role: .destructive) {
                        Task { await model.unassign(slotKey: utterance.slotKey); dismiss() }
                    }
                }
            }
        }
        if !utterance.slotKey.isEmpty && utterance.personId != "me" {
            Section {
                // 2026-09-27 用户：「只用一个从已登记表中选择的选项就好」
                let registered = model.people.filter { !$0.isUser && $0.id != utterance.personId }
                if !registered.isEmpty {
                    Picker("从已登记的人里选", selection: $picked) {
                        Text("请选择").tag("")
                        ForEach(registered) { person in Text(person.name).tag(person.name) }
                    }
                    .onChange(of: picked) { _, value in
                        guard !value.isEmpty else { return }
                        name = value
                        submit()
                    }
                }
                HStack {
                    TextField("或者输入新名字", text: $name)
                    Button("定为新的人") { submit() }
                        .disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                if !note.isEmpty { Text(note).font(.caption).foregroundStyle(.red) }
            } header: {
                Text(utterance.personId == nil ? "这是谁？" : "其实是别人？")
            } footer: {
                Text("这个声音块的全部话都会归到选中的人。")
            }
            if utterance.personId == nil {
                Section {
                    Button("删除「\(utterance.displayName)」说过的全部话", role: .destructive) { confirmDeleteSlot = true }
                        .disabled(busy)
                }
            }
        }
    }

    private func submit() {
        let target = name.trimmingCharacters(in: .whitespaces)
        guard !target.isEmpty else { return }
        busy = true
        Task {
            if let failure = await model.assign(slotKey: utterance.slotKey, name: target) {
                note = failure
            } else {
                dismiss()
            }
            busy = false
        }
    }
}

// MARK: - 人物资料

struct NativeAmbientPersonView: View {
    let personId: String
    let onChange: () -> Void

    @State private var person: NativeAmbientPersonInfo?
    @State private var history: [[String: Any]] = []
    @State private var name = ""
    @State private var intro = ""
    @State private var profile = ""
    @State private var language = ""
    @State private var message = ""
    @State private var busy = false
    @State private var others: [NativeAmbientPersonInfo] = []
    @State private var mergeTarget: NativeAmbientPersonInfo?
    @State private var confirmDelete = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        List {
            if let person {
                if person.isUser {
                    Section { Text("这是你自己。声纹在「旁听与降噪」里登记。").foregroundStyle(.secondary) }
                } else {
                    editSections(person)
                }
            } else {
                ProgressView("读取中…")
            }
            if !message.isEmpty { Section { Text(message).font(.caption).foregroundStyle(.secondary) } }
            historySection
        }
        .navigationTitle(person?.name ?? "人物")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .confirmationDialog("合并", isPresented: Binding(get: { mergeTarget != nil }, set: { if !$0 { mergeTarget = nil } }),
                            presenting: mergeTarget) { target in
            Button("把「\(person?.name ?? "")」合并进「\(target.name)」", role: .destructive) { merge(into: target) }
        } message: { target in
            Text("两个人的 KJ 页、对话记录、介绍、AI 整理、声纹都会合到「\(target.name)」。")
        }
        .confirmationDialog("删除人物", isPresented: $confirmDelete) {
            Button("删除「\(person?.name ?? "")」和他说过的全部话", role: .destructive) { deletePerson(purge: true) }
            Button("只删人物（他的话留在时间轴上）", role: .destructive) { deletePerson(purge: false) }
        } message: {
            Text("都会删掉他的声纹、从人物列表里去掉。KJ 人物页（Obsidian）保留。")
        }
    }

    @ViewBuilder private func editSections(_ person: NativeAmbientPersonInfo) -> some View {
        Section {
            TextField("名字", text: $name)
            if !person.aliases.isEmpty {
                LabeledContent("别名", value: person.aliases.joined(separator: "、"))
            }
        } header: { Text("名字") } footer: {
            Text("改成另一个已有的人的名字 = 合并成同一个人（KJ 节点一起合并）。")
        }
        languageSection(person)
        Section {
            TextEditor(text: $intro).frame(minHeight: 70)
        } header: { Text("我的介绍") } footer: {
            Text("自己写的。jev 判断旁听内容时会拿它当线索。")
        }
        Section {
            TextEditor(text: $profile).frame(minHeight: 120)
            Button("让 AI 按对话历史重新整理") { summarize() }.disabled(busy)
        } header: { Text("AI 整理（关系 / 商量过 / 近况）") } footer: {
            Text("有新对话攒够 5 段会自动重写；你也可以直接改。保存在 KJ 人物页里（Obsidian）。")
        }
        Section {
            Button("保存") { save() }.disabled(busy)
            Menu("与另一个人是同一个人…") {
                ForEach(others) { other in
                    Button(other.name) { mergeTarget = other }
                }
            }
            .disabled(others.isEmpty)
            LabeledContent("声音块 / 声纹", value: "\(person.slots) / \(person.voiceprints)")
        }
        Section {
            Button("删除这个人", role: .destructive) { confirmDelete = true }.disabled(busy)
        } footer: {
            Text("可以连同他在时间轴上说过的全部话一起删，也可以只删人物。KJ 人物页（Obsidian）保留。")
        }
    }

    @ViewBuilder private func languageSection(_ person: NativeAmbientPersonInfo) -> some View {
        Section {
            Picker("说的语言", selection: $language) {
                Text("未登记（逐段推测）").tag("")
                ForEach(NativeSegmentTranscriber.allLocales, id: \.self) { code in
                    Text(NativeSegmentTranscriber.displayName(code)).tag(code)
                }
            }
            if person.language.isEmpty && !person.languageGuess.isEmpty {
                let votes = person.languageVotes.sorted { $0.value > $1.value }
                    .map { "\(NativeSegmentTranscriber.displayName($0.key)) \($0.value) 段" }.joined(separator: "，")
                Text("推测：\(votes)").font(.caption).foregroundStyle(.secondary)
                Button("确认他说\(NativeSegmentTranscriber.displayName(person.languageGuess))") {
                    language = person.languageGuess
                    save()
                }
            }
        } header: { Text("语言") } footer: {
            Text("登记后他的话都用这种语言转写；没登记时每段在候选语言里推测，结果标「推测」。")
        }
    }

    private var historySection: some View {
        Section("对话历史（\(history.count) 段）") {
            ForEach(Array(history.enumerated()), id: \.offset) { _, window in
                let lines = window["lines"] as? [[String: Any]] ?? []
                let t0 = (window["t0"] as? NSNumber)?.doubleValue ?? 0
                DisclosureGroup {
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                        let who = (line["name"] as? String) ?? (line["label"] as? String) ?? "?"
                        let key = (line["personId"] as? String).map { "p:" + $0 } ?? "s:" + ((line["slotKey"] as? String) ?? "")
                        HStack(alignment: .top, spacing: 6) {
                            Text(who).font(.caption.bold()).foregroundStyle(NativeAmbientPalette.color(for: key))
                                .frame(width: 64, alignment: .leading)
                            Text(line["text"] as? String ?? "").font(.callout).textSelection(.enabled)
                        }
                    }
                } label: {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(Date(timeIntervalSince1970: t0 / 1000).formatted(date: .abbreviated, time: .shortened))
                            .font(.caption.bold())
                        Text(lines.map { $0["text"] as? String ?? "" }.joined(separator: " "))
                            .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
            }
        }
    }

    private func load() async {
        do {
            let reply = try await NativeAmbientServer.get("api/ambient/people/\(personId)")
            guard let info = (reply["person"] as? [String: Any]).flatMap(NativeAmbientPersonInfo.init) else { return }
            person = info
            name = info.name
            intro = info.intro
            profile = info.profile
            language = info.language
            history = reply["history"] as? [[String: Any]] ?? []
            let list = try await NativeAmbientServer.get("api/ambient/people")
            others = (list["people"] as? [[String: Any]] ?? []).compactMap(NativeAmbientPersonInfo.init)
                .filter { $0.id != info.id && !$0.isUser }
        } catch {
            message = "读取失败：\(error.localizedDescription)"
            NativeAmbientLog.note("人物页：读取失败 \(error.localizedDescription)", level: "error")
        }
    }

    private func save() {
        guard let person else { return }
        busy = true
        Task {
            var body: [String: Any] = [:]
            if name != person.name { body["name"] = name }
            if intro != person.intro { body["intro"] = intro }
            if profile != person.profile { body["profile"] = profile }
            if language != person.language { body["language"] = language }
            defer { busy = false }
            guard !body.isEmpty else { message = "没有改动"; return }
            do {
                let reply = try await NativeAmbientServer.patch("api/ambient/people/\(person.id)", body: body)
                let merged = ((reply["person"] as? [String: Any])?["id"] as? String).map { $0 != person.id } ?? false
                message = merged ? "已合并到同名的人" : "已保存（KJ 人物页已更新）"
                await NativeSpeakerEmbedder.shared.invalidateServer()
                onChange()
                if !merged { await load() }
            } catch {
                message = "保存失败：\(error.localizedDescription)"
                NativeAmbientLog.note("人物页：保存失败 \(error.localizedDescription)", level: "error")
            }
        }
    }

    private func summarize() {
        busy = true
        message = "AI 整理中…（要十几秒到一分钟）"
        Task {
            defer { busy = false }
            do {
                let reply = try await NativeAmbientServer.post("api/ambient/people/\(personId)/summarize", body: [:], timeout: 180)
                profile = reply["profile"] as? String ?? profile
                message = "已按对话历史重新整理"
                onChange()
            } catch {
                message = "整理失败：\(error.localizedDescription)"
            }
        }
    }

    private func deletePerson(purge: Bool) {
        busy = true
        Task {
            defer { busy = false }
            do {
                _ = try await NativeAmbientServer.delete("api/ambient/people/\(personId)" + (purge ? "?purge=1" : ""))
                await NativeSpeakerEmbedder.shared.invalidateServer()
                NativeAmbientLog.note("人物：已删除 \(personId)")
                onChange()
                dismiss()
            } catch {
                message = "删除失败：\(error.localizedDescription)"
                NativeAmbientLog.note("人物：删除失败 \(error.localizedDescription)", level: "error")
            }
        }
    }

    private func merge(into target: NativeAmbientPersonInfo) {
        busy = true
        Task {
            defer { busy = false }
            do {
                _ = try await NativeAmbientServer.post("api/ambient/people/\(personId)/merge", body: ["into": target.id])
                message = "已合并进「\(target.name)」"
                await NativeSpeakerEmbedder.shared.invalidateServer()
                onChange()
            } catch {
                message = "合并失败：\(error.localizedDescription)"
            }
        }
    }
}
