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

    init?(_ row: [String: Any]) {
        guard let id = row["id"] as? String, let t0 = (row["t0"] as? NSNumber)?.doubleValue else { return nil }
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

    init?(_ row: [String: Any]) {
        guard let id = row["id"] as? String else { return nil }
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

struct NativeAmbientLane: Identifiable, Hashable {
    let key: String
    let name: String
    var id: String { key }
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

    /// 泳道顺序：「我」在最上，其余按第一次出现的时间。
    var lanes: [NativeAmbientLane] {
        var seen: [String: String] = [:]
        var order: [String] = []
        for u in utterances where seen[u.laneKey] == nil {
            seen[u.laneKey] = u.displayName
            order.append(u.laneKey)
        }
        if let me = order.firstIndex(of: "p:me") { order.insert(order.remove(at: me), at: 0) }
        return order.map { NativeAmbientLane(key: $0, name: seen[$0] ?? "?") }
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
    @State private var scale: CGFloat = 6          // 每秒多少点
    @State private var selected: NativeAmbientUtterance?

    var body: some View {
        NavigationStack {
            List {
                controls
                if let error = model.error { Section { Text(error).foregroundStyle(.red) } }
                Section("时间轴") { timeline }
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
            .onChange(of: model.range) { _, _ in Task { await model.reload() } }
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
            HStack {
                Image(systemName: "minus.magnifyingglass")
                Slider(value: $scale, in: 1...40)
                Image(systemName: "plus.magnifyingglass")
            }
        } footer: {
            Text("每一行是一个人（设成同一个名字的声音块合在一行），每个色块是一句话；超过 30 秒的静音被压缩成一道竖线。点色块查看与编辑。")
        }
    }

    @ViewBuilder private var timeline: some View {
        if model.loading && model.utterances.isEmpty {
            ProgressView("读取中…")
        } else if model.utterances.isEmpty {
            Text("这段时间没有旁听记录。").foregroundStyle(.secondary)
        } else {
            NativeAmbientTimelineCanvas(utterances: model.utterances, lanes: model.lanes, scale: scale) { tapped in
                selected = tapped
            }
            .frame(height: CGFloat(max(1, model.lanes.count)) * NativeAmbientTimelineCanvas.laneHeight + 28)
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
            }
        }
    }
}

// MARK: - 画布

/// 压缩时间轴：连续说话段内按秒等比例，段与段之间（静音 > 30 秒）固定留一道窄缝并标时间。
struct NativeAmbientTimelineCanvas: View {
    static let laneHeight: CGFloat = 34
    static let labelWidth: CGFloat = 76
    static let gapWidth: CGFloat = 28
    static let gapThreshold: Double = 30_000

    let utterances: [NativeAmbientUtterance]
    let lanes: [NativeAmbientLane]
    let scale: CGFloat
    let onTap: (NativeAmbientUtterance) -> Void

    private struct Cluster {
        let start: Double
        let end: Double
        let x: CGFloat
    }

    private var clusters: [Cluster] {
        var out: [Cluster] = []
        var x: CGFloat = 8
        var start = utterances.first?.t0 ?? 0
        var end = start
        for u in utterances.sorted(by: { $0.t0 < $1.t0 }) {
            if u.t0 - end > Self.gapThreshold {
                out.append(Cluster(start: start, end: end, x: x))
                x += CGFloat((end - start) / 1000) * scale + Self.gapWidth
                start = u.t0
            }
            end = max(end, u.t1)
        }
        out.append(Cluster(start: start, end: end, x: x))
        return out
    }

    private var laneIndex: [String: Int] {
        var out: [String: Int] = [:]
        for (offset, lane) in lanes.enumerated() { out[lane.key] = offset }
        return out
    }

    private func xPosition(_ t: Double, in clusters: [Cluster]) -> CGFloat {
        let cluster = clusters.last { $0.start <= t } ?? clusters[0]
        return cluster.x + CGFloat((t - cluster.start) / 1000) * scale
    }

    var body: some View {
        let clusters = self.clusters
        let last = clusters.last
        let width = (last.map { $0.x + CGFloat(($0.end - $0.start) / 1000) * scale } ?? 0) + 24
        let laneIndex = self.laneIndex
        HStack(alignment: .top, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                Color.clear.frame(height: 20)
                ForEach(lanes) { lane in
                    Text(lane.name)
                        .font(.caption.bold())
                        .foregroundStyle(NativeAmbientPalette.color(for: lane.key))
                        .lineLimit(1)
                        .frame(width: Self.labelWidth, height: Self.laneHeight, alignment: .leading)
                }
            }
            ScrollView(.horizontal) {
                ZStack(alignment: .topLeading) {
                    Color.clear.frame(width: width, height: CGFloat(lanes.count) * Self.laneHeight + 20)
                    ForEach(Array(clusters.enumerated()), id: \.offset) { index, cluster in
                        clusterMark(cluster, first: index == 0)
                    }
                    ForEach(utterances) { u in
                        block(u, lane: laneIndex[u.laneKey] ?? 0, clusters: clusters)
                    }
                }
            }
            .defaultScrollAnchor(.trailing)
        }
    }

    private func clusterMark(_ cluster: Cluster, first: Bool) -> some View {
        let label = Date(timeIntervalSince1970: cluster.start / 1000).formatted(date: .omitted, time: .shortened)
        return ZStack(alignment: .topLeading) {
            if !first {
                Rectangle().fill(Color.secondary.opacity(0.35))
                    .frame(width: 1, height: CGFloat(lanes.count) * Self.laneHeight + 20)
                    .offset(x: cluster.x - Self.gapWidth / 2)
            }
            Text(label).font(.caption2).foregroundStyle(.secondary).offset(x: cluster.x)
        }
    }

    private func block(_ u: NativeAmbientUtterance, lane: Int, clusters: [Cluster]) -> some View {
        let x = xPosition(u.t0, in: clusters)
        let w = max(6, CGFloat((u.t1 - u.t0) / 1000) * scale)
        let color = NativeAmbientPalette.color(for: u.laneKey)
        return RoundedRectangle(cornerRadius: 4)
            .fill(color.opacity(u.personId == nil ? 0.45 : 0.85))
            .frame(width: w, height: Self.laneHeight - 10)
            .overlay(alignment: .leading) {
                if w > 40 {
                    Text(u.text).font(.caption2).foregroundStyle(.white).lineLimit(1).padding(.horizontal, 4)
                }
            }
            .offset(x: x, y: 20 + CGFloat(lane) * Self.laneHeight + 5)
            .onTapGesture { onTap(u) }
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
    @State private var message = ""
    @State private var busy = false
    @State private var others: [NativeAmbientPersonInfo] = []
    @State private var mergeTarget: NativeAmbientPersonInfo?

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
