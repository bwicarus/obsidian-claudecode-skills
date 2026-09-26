import SwiftUI

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
    var displayName: String { name ?? label }
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

@MainActor
final class NativeAmbientTimelineModel: ObservableObject {
    enum Span: String, CaseIterable, Identifiable {
        case hour = "1 小时", threeHours = "3 小时", today = "今天", yesterday = "昨天"
        var id: String { rawValue }
    }

    @Published var range: Span = .threeHours
    /// 起始点：0 = 范围的开头，1 = 现在。只看这之后的话（2026-09-27 用户：「时间轴滑条可以调整起始点」）。
    @Published var startFraction: Double = 0
    @Published private(set) var utterances: [NativeAmbientUtterance] = []
    @Published private(set) var people: [NativeAmbientPersonInfo] = []
    @Published private(set) var loading = false
    @Published var error: String?

    func bounds() -> (Double, Double) {
        let now = Date()
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
        loading = true
        defer { loading = false }
        let (from, to) = bounds()
        do {
            let timeline = try await NativeAmbientServer.get("api/ambient/timeline?from=\(Int(from))&to=\(Int(to))")
            utterances = (timeline["utterances"] as? [[String: Any]] ?? []).compactMap(NativeAmbientUtterance.init)
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
        return from + (to - from) * startFraction
    }

    /// 一段对话：中间停顿超过 60 秒就另起一段。段与段、段内的句子都是新的在上。
    struct Group: Identifiable {
        let id: String
        let lines: [NativeAmbientUtterance]
        var start: Double { lines.last?.t0 ?? 0 }
        var end: Double { lines.first?.t1 ?? 0 }
        var speakers: Int { Set(lines.map(\.laneKey)).count }
    }

    var groups: [Group] {
        let start = startTime
        let visible = utterances.filter { $0.t1 >= start }.sorted { $0.t0 > $1.t0 }
        var out: [[NativeAmbientUtterance]] = []
        for u in visible {
            if let newer = out.last?.last, newer.t0 - u.t1 <= 60_000 { out[out.count - 1].append(u) }
            else { out.append([u]) }
        }
        return out.map { Group(id: $0.first?.id ?? UUID().uuidString, lines: $0) }
    }

    func deletePerson(_ id: String) async -> String? {
        do {
            let reply = try await NativeAmbientServer.delete("api/ambient/people/\(id)")
            await NativeSpeakerEmbedder.shared.invalidateServer()
            NativeAmbientLog.note("人物：已删除 \(id)（声纹 \(reply["voiceprints"] ?? 0) 条、声音块 \(reply["slots"] ?? 0) 个退回未定人）")
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
            }
            .task { await model.reload() }
            .onChange(of: model.range) { _, _ in model.startFraction = 0; Task { await model.reload() } }
            .confirmationDialog("删除人物", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }),
                                presenting: deleting) { person in
                Button("删除「\(person.name)」", role: .destructive) {
                    Task { if let failure = await model.deletePerson(person.id) { model.error = "删除失败：\(failure)" } }
                }
            } message: { person in
                Text("删掉他的声纹，他名下的声音块退回「说话人N」，人物列表里不再显示。KJ 人物页和过去的对话记录保留（要彻底删在 Obsidian 里删那一页）。")
            }
            .sheet(item: $selected) { utterance in
                NativeAmbientBlockSheet(utterance: utterance, model: model)
            }
        }
    }

    private var controls: some View {
        Section {
            Picker("范围", selection: $model.range) {
                ForEach(NativeAmbientTimelineModel.Span.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("起始点").font(.subheadline)
                    Spacer()
                    Text(Date(timeIntervalSince1970: model.startTime / 1000).formatted(date: .omitted, time: .shortened))
                        .font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                }
                Slider(value: $model.startFraction, in: 0...1)
            }
        } footer: {
            Text("新的在上。只显示起始点之后的话；停顿超过 1 分钟另起一段。点一句查看、给说话人定名字。")
        }
    }

    @ViewBuilder private var timeline: some View {
        if model.loading && model.utterances.isEmpty {
            Section { ProgressView("读取中…") }
        } else if model.groups.isEmpty {
            Section { Text("这段时间没有旁听记录。").foregroundStyle(.secondary) }
        } else {
            ForEach(model.groups) { group in
                Section {
                    ForEach(Array(group.lines.enumerated()), id: \.element.id) { index, line in
                        let sameSpeaker = index > 0 && group.lines[index - 1].laneKey == line.laneKey
                        NativeAmbientTimelineRow(utterance: line, showName: !sameSpeaker)
                            .contentShape(Rectangle())
                            .onTapGesture { selected = line }
                    }
                } header: {
                    let start = Date(timeIntervalSince1970: group.start / 1000)
                    let end = Date(timeIntervalSince1970: group.end / 1000)
                    Text("\(start.formatted(date: .abbreviated, time: .shortened)) – \(end.formatted(date: .omitted, time: .shortened))"
                         + " · \(group.lines.count) 句 · \(group.speakers) 人")
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

/// 时间轴的一行：时间 · 说话人 · 整句话（长句换行显示完整）。同一个人连着说时名字只标第一行。
struct NativeAmbientTimelineRow: View {
    let utterance: NativeAmbientUtterance
    let showName: Bool

    var body: some View {
        let color = NativeAmbientPalette.color(for: utterance.laneKey)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(Date(timeIntervalSince1970: utterance.t0 / 1000).formatted(date: .omitted, time: .standard))
                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                .frame(width: 62, alignment: .leading)
            Text(showName ? utterance.displayName : "")
                .font(.caption.bold()).foregroundStyle(color).lineLimit(1)
                .frame(width: 70, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(utterance.text).font(.callout).fixedSize(horizontal: false, vertical: true)
                if !utterance.lang.isEmpty && utterance.lang != "zh-CN" {
                    Text(NativeSegmentTranscriber.displayName(utterance.lang) + (utterance.langConfirmed ? "" : "（推测）"))
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.leading, 6)
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 1.5).fill(color.opacity(utterance.personId == nil ? 0.45 : 0.9)).frame(width: 3)
        }
    }
}

// MARK: - 点块

struct NativeAmbientBlockSheet: View {
    let utterance: NativeAmbientUtterance
    @ObservedObject var model: NativeAmbientTimelineModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var note = ""
    @State private var busy = false

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
                TextField("名字", text: $name)
                let suggestions = model.people.filter { !$0.isUser && ($0.name.contains(name) || name.isEmpty) }.prefix(6)
                ForEach(Array(suggestions)) { person in
                    Button("是「\(person.name)」") { name = person.name; submit() }
                }
                Button(utterance.personId == nil ? "定为这个人" : "改成这个人") { submit() }
                    .disabled(busy || name.trimmingCharacters(in: .whitespaces).isEmpty)
                if !note.isEmpty { Text(note).font(.caption).foregroundStyle(.red) }
            } header: {
                Text(utterance.personId == nil ? "这是谁？" : "其实是别人？")
            } footer: {
                Text("填已有的名字就合到那个人（同一个人）；填新名字会在 KJ 里新建人物。这个声音块的全部话都跟着改名。")
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
            Button("删除「\(person?.name ?? "")」", role: .destructive) { deletePerson() }
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
            Text("删掉他的声纹，他名下的声音块退回「说话人N」，人物列表里不再显示。KJ 人物页和过去的对话记录保留。")
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

    private func deletePerson() {
        busy = true
        Task {
            defer { busy = false }
            do {
                _ = try await NativeAmbientServer.delete("api/ambient/people/\(personId)")
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
