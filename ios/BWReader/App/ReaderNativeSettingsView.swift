import SwiftUI

@MainActor
final class ReaderNativeSettingsModel: ObservableObject, Identifiable {
    let id = UUID()
    private let scope: String
    private let request: ([String: Any]) async -> [String: Any]
    @Published private(set) var models: [String: Any] = [:]
    @Published private(set) var fields: [ReaderNativeSettingField] = []
    @Published private(set) var profiles: [String] = []
    @Published private(set) var activeProfile = ""
    @Published private(set) var computer: [String: Any] = [:]
    @Published private(set) var loading = Set<String>()
    @Published private(set) var saving = false
    @Published private(set) var error: String?
    @Published private(set) var notice: String?

    init(scope: String, request: @escaping ([String: Any]) async -> [String: Any]) {
        self.scope = scope
        self.request = request
    }

    func load(_ section: String) async {
        guard !loading.contains(section) else { return }
        loading.insert(section)
        defer { loading.remove(section) }
        let receipt = await request(["action": "settingsRead", "scope": scope, "section": section])
        guard !Task.isCancelled else { return }
        guard receipt["ok"] as? Bool == true, let value = receipt["value"] as? [String: Any] else {
            error = receipt["error"] as? String ?? "配置读取失败"
            return
        }
        switch section {
        case "models": models = value
        case "voice": fields = (value["fields"] as? [[String: Any]] ?? []).compactMap(ReaderNativeSettingField.init)
        case "profiles": profiles = value["profiles"] as? [String] ?? []; activeProfile = value["active"] as? String ?? ""
        case "computer": computer = value
        default: break
        }
    }

    func reload() async {
        error = nil
        async let modelRead: Void = load("models")
        async let voiceRead: Void = load("voice")
        async let profileRead: Void = load("profiles")
        _ = await (modelRead, voiceRead, profileRead)
    }

    @discardableResult
    func save(section: String, values: [String: Any]) async -> Bool {
        guard !saving else { return false }
        saving = true
        error = nil
        notice = nil
        defer { saving = false }
        var command = values
        command["action"] = "settingsWrite"
        command["scope"] = scope
        command["section"] = section
        let receipt = await request(command)
        guard !Task.isCancelled else { return false }
        guard receipt["ok"] as? Bool == true else {
            error = receipt["error"] as? String ?? "设置未确认保存，请重新读取后核对。"
            return false
        }
        notice = "已保存"
        await load(section)
        if section == "models" || section == "profiles" {
            await load(section == "models" ? "profiles" : "models")
        }
        return true
    }
}

struct ReaderNativeSettingField: Identifiable {
    var id: String { key }
    let key: String
    let label: String
    let kind: String
    let section: String
    let value: Any
    let options: [[String: String]]
    let minimum: Double
    let maximum: Double
    let step: Double
    let device: Bool
    let disabled: Bool

    var summary: String {
        if kind == "toggle" { return (value as? Bool == true) ? "开启" : "关闭" }
        let string = value as? String ?? (value as? NSNumber)?.stringValue ?? ""
        return options.first(where: { $0["value"] == string })?["label"] ?? (string.isEmpty ? "默认" : string)
    }

    init?(_ value: [String: Any]) {
        guard let key = value["key"] as? String, let kind = value["kind"] as? String else { return nil }
        self.key = key
        self.kind = kind
        self.value = value["value"] ?? ""
        label = value["label"] as? String ?? key
        section = value["section"] as? String ?? "通话"
        options = value["options"] as? [[String: String]] ?? []
        minimum = (value["min"] as? NSNumber)?.doubleValue ?? 0
        maximum = (value["max"] as? NSNumber)?.doubleValue ?? 100
        step = (value["step"] as? NSNumber)?.doubleValue ?? 1
        device = value["device"] as? Bool ?? false
        disabled = value["disabled"] as? Bool ?? false
    }
}

@MainActor
struct ReaderNativeSettingsView: View {
    @ObservedObject var model: ReaderNativeSettingsModel
    @Environment(\.dismiss) private var dismiss
    @State private var tab = "models"
    @State private var naming = false
    @State private var profileName = ""
    @State private var deleting: String?

    var body: some View {
        NavigationStack {
            List {
                if let error = model.error {
                    Section { Text(error).foregroundStyle(.red).textSelection(.enabled) }
                }
                if let notice = model.notice { Section { Text(notice).foregroundStyle(ReaderNativeTheme.accent) } }
                Section {
                    Picker("设置分类", selection: $tab) {
                        Text("阅读 AI").tag("models")
                        Text("语音与朗读").tag("voice")
                        Text("电脑通话").tag("computer")
                    }.pickerStyle(.segmented)
                }
                if tab == "models" { modelSections }
                else if tab == "voice" { voiceSections }
                else { computerSections }
            }
            .scrollContentBackground(.hidden).background(ReaderNativeTheme.canvas)
            .navigationTitle("模型与声音").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
                ToolbarItem(placement: .topBarLeading) {
                    Button("刷新", systemImage: "arrow.clockwise") {
                        Task {
                            if tab == "computer" { await model.load("computer") }
                            else { await model.reload() }
                        }
                    }
                        .disabled(model.saving || !model.loading.isEmpty)
                }
            }
            .task { await model.reload() }
            .task(id: tab) { if tab == "computer" { await model.load("computer") } }
            .alert("保存当前设置为预设", isPresented: $naming) {
                TextField("名称（最多 20 字）", text: $profileName)
                Button("取消", role: .cancel) { }
                Button("保存") { Task { await model.save(section: "profiles", values: ["op": "save", "name": profileName]) } }
            }
            .alert("删除预设", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
                Button("取消", role: .cancel) { deleting = nil }
                Button("删除", role: .destructive) {
                    let name = deleting ?? ""
                    deleting = nil
                    Task { await model.save(section: "profiles", values: ["op": "delete", "name": name]) }
                }
            } message: { Text("删除“\(deleting ?? "")”？当前生效的模型配置会保留。") }
        }.tint(ReaderNativeTheme.accent)
    }

    @ViewBuilder private var modelSections: some View {
        Section("预设") {
            ForEach(model.profiles, id: \.self) { name in
                Button {
                    Task { await model.save(section: "profiles", values: ["op": "apply", "name": name]) }
                } label: {
                    HStack {
                        Text(name)
                        Spacer()
                        if name == model.activeProfile { Image(systemName: "checkmark") }
                    }
                }
                .disabled(model.saving)
                .swipeActions { Button("删除", role: .destructive) { deleting = name } }
            }
            Button("保存当前为预设", systemImage: "plus") { profileName = ""; naming = true }
                .disabled(model.saving)
        }
        Section("按任务配置") {
            if model.loading.contains("models") && model.models.isEmpty { ProgressView("读取模型目录…") }
            let actions = model.models["actions"] as? [String: [String: Any]] ?? [:]
            let names = model.models["names"] as? [String: String] ?? [:]
            ForEach(actions.keys.sorted(), id: \.self) { key in
                if let info = actions[key] {
                    NavigationLink {
                        ReaderNativeTaskPreferenceEditor(action: key, label: names[key] ?? key, info: info, model: model)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(names[key] ?? key)
                            let current = info["pref"] as? [String: Any] ?? info["default"] as? [String: Any] ?? [:]
                            Text([current["variant"] as? String, current["depth"] as? String].compactMap { $0 }.joined(separator: " · "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private var voiceSections: some View {
        if model.loading.contains("voice") && model.fields.isEmpty { ProgressView("读取语音配置…") }
        ForEach(["通话", "朗读与输入", "本设备"], id: \.self) { section in
            Section(section) {
                ForEach(model.fields.filter { $0.section == section }) { field in
                    NavigationLink {
                        ReaderNativeSettingEditor(field: field, model: model)
                    } label: {
                        HStack {
                            Text(field.label)
                            Spacer()
                            Text(field.summary).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }.disabled(field.disabled || model.saving)
                }
            }
        }
        Section {
            Text("普通语音的模型、语言和人设在下次通话生效。声音与语速沿用现有通话链路的更新能力。任务提示音与工具口头回报互斥。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var computerSections: some View {
        let value = model.computer
        let busy = value["busy"] as? Bool == true
        let target = value["target"] as? String
        let status = value["status"] as? [String: Any] ?? [:]
        let voice = status["codexVoice"] as? [String: Any] ?? [:]
        Section("语音与文字接力目标") {
            if model.loading.contains("computer") { ProgressView("读取电脑通话状态…") }
            ForEach(["codex-desktop", "chatgpt-classic"], id: \.self) { item in
                Button {
                    Task { await model.save(section: "computer", values: ["value": item]) }
                } label: {
                    HStack {
                        Text(item == "codex-desktop" ? "Codex" : "GPT Classic")
                        Spacer()
                        if target == item { Image(systemName: "checkmark") }
                    }
                }.disabled(busy || model.saving || model.loading.contains("computer") || target == nil)
            }
            if busy { Text("结束当前电脑语音后可切换目标。").font(.caption).foregroundStyle(.secondary) }
            Text(target == "chatgpt-classic"
                 ? "音频连接 GPT Classic；文字接力沿用旧版文字注入开关。阅读快照与卡片工具仍由 Codex 提供。"
                 : "电脑按钮连接 Codex 音频；阅读快照与卡片工具由 ReaderPC 提供。")
                .font(.caption).foregroundStyle(.secondary)
        }
        Section("连接状态") {
            LabeledContent("Windows 桥接", value: computerConnectionLabel(value))
            LabeledContent("Codex 语音", value: computerVoiceLabel(value, voice: voice))
            if let reason = value["reason"] as? String, !reason.isEmpty {
                Text(reason).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            ForEach(value["errors"] as? [String] ?? [], id: \.self) { message in
                Text(message).foregroundStyle(.red).textSelection(.enabled)
            }
        }
        if let failure = status["lastError"] as? [String: Any], !failure.isEmpty {
            computerErrorSection("最近 Windows 错误", failure: failure)
        }
        if let failure = value["clientError"] as? [String: Any], !failure.isEmpty {
            computerErrorSection("最近连接错误", failure: failure)
        }
        Section {
            Text("刷新只读取连接状态，切换目标在下次连接生效。点击侧栏电脑按钮才建立音频连接；挂断停止音频桥接。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func computerConnectionLabel(_ value: [String: Any]) -> String {
        switch value["state"] as? String {
        case "ready": return "已就绪"
        case "active", "running": return "已连接"
        case "idle": return (value["status"] as? [String: Any])?["ready"] as? Bool == true ? "已就绪" : "空闲"
        case "offline": return "离线或电脑休眠"
        case "busy": return "忙碌"
        default: return "暂未取得状态"
        }
    }

    private func computerVoiceLabel(_ value: [String: Any], voice: [String: Any]) -> String {
        if value["voiceEnabled"] as? Bool == false { return "已关闭" }
        if voice["status"] as? String == "available" {
            return voice["active"] as? Bool == true ? "正在运行" : "未运行"
        }
        if voice["status"] as? String == "error" { return "读取失败" }
        return "暂未取得状态"
    }

    private func computerErrorSection(_ title: String, failure: [String: Any]) -> some View {
        Section(title) {
            ForEach(["code", "stage", "message", "hresult", "failureId", "atUtc", "at"], id: \.self) { key in
                if let text = failure[key] as? String, !text.isEmpty {
                    Text(text).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
        }
    }
}

@MainActor
private struct ReaderNativeSettingEditor: View {
    let field: ReaderNativeSettingField
    @ObservedObject var model: ReaderNativeSettingsModel
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @State private var number: Double
    @State private var enabled: Bool

    init(field: ReaderNativeSettingField, model: ReaderNativeSettingsModel) {
        self.field = field
        self.model = model
        _text = State(initialValue: field.value as? String ?? "")
        _number = State(initialValue: (field.value as? NSNumber)?.doubleValue ?? field.minimum)
        _enabled = State(initialValue: field.value as? Bool ?? false)
    }

    var body: some View {
        Form {
            if let error = model.error { Text(error).foregroundStyle(.red) }
            Section(field.label) {
                if field.kind == "choice" {
                    Picker(field.label, selection: $text) {
                        if !field.options.contains(where: { $0["value"] == text }) {
                            Text("当前值：\(text)").tag(text).disabled(true)
                        }
                        ForEach(field.options.indices, id: \.self) { index in
                            Text(field.options[index]["label"] ?? "").tag(field.options[index]["value"] ?? "")
                        }
                    }.pickerStyle(.inline)
                } else if field.kind == "toggle" { Toggle(field.label, isOn: $enabled) }
                else if field.kind == "range" {
                    Text(number, format: .number.precision(.fractionLength(0...2))).monospacedDigit()
                    Slider(value: $number, in: field.minimum...field.maximum, step: field.step)
                } else { TextField("留空使用默认", text: $text, axis: .vertical).lineLimit(3...12) }
            }
        }
        .navigationTitle(field.label).navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("保存") {
                    var value: Any = text
                    if field.kind == "toggle" { value = enabled }
                    else if field.kind == "range" { value = number }
                    Task {
                        if await model.save(section: "voice", values: ["key": field.key, "value": value, "device": field.device]) { dismiss() }
                    }
                }.disabled(model.saving)
            }
        }
    }
}

@MainActor
private struct ReaderNativeTaskPreferenceEditor: View {
    let action: String
    let label: String
    let info: [String: Any]
    @ObservedObject var model: ReaderNativeSettingsModel
    @Environment(\.dismiss) private var dismiss
    @State private var backend: String
    @State private var variant: String
    @State private var depth: String
    @State private var fast: Bool

    init(action: String, label: String, info: [String: Any], model: ReaderNativeSettingsModel) {
        self.action = action
        self.label = label
        self.info = info
        self.model = model
        let value = info["pref"] as? [String: Any] ?? info["default"] as? [String: Any] ?? [:]
        _backend = State(initialValue: value["backend"] as? String ?? "")
        _variant = State(initialValue: value["variant"] as? String ?? "")
        _depth = State(initialValue: value["depth"] as? String ?? "")
        _fast = State(initialValue: value["fast"] as? Bool ?? false)
    }

    private var catalog: [String: Any] { model.models["catalog"] as? [String: Any] ?? [:] }
    private var backends: [String] {
        (catalog["backends_by_action"] as? [String: [String]])?[action] ?? catalog["backends"] as? [String] ?? []
    }
    private var locked: [String] { (model.models["locked"] as? [String: [String]])?[action] ?? [] }
    private var variants: [String] {
        var values = (catalog["variants"] as? [String: [String]])?[backend] ?? []
        if !variant.isEmpty && !values.contains(variant) { values.insert(variant, at: 0) }
        return values
    }
    private var depths: [String] {
        if backend == "codex", let list = (catalog["codex_depths_by_model"] as? [String: [String]])?[variant] { return list }
        return (catalog["depths"] as? [String: [String]])?[backend] ?? []
    }
    private func capability(_ value: String) -> [String: Any] {
        (catalog["codex_capabilities"] as? [String: [String: Any]])?[value] ?? [:]
    }
    private var fastSupported: Bool {
        backend == "codex" && capability(variant)["selectable"] as? Bool == true &&
            (capability(variant)["fast"] as? Bool == true || capability(variant)["priority"] as? Bool == true)
    }
    private func selectable(_ value: String) -> Bool {
        let known = (catalog["variants"] as? [String: [String]])?[backend] ?? []
        if backend == "codex" { return capability(value)["selectable"] as? Bool == true }
        return known.contains(value) || (backend == "gemini" && value.hasSuffix("@paid") && known.contains(String(value.dropLast(5))))
    }
    private func modelLabel(_ value: String) -> String {
        let name = (catalog["variant_short"] as? [String: String])?[value] ?? value
        if backend == "codex", let reason = capability(value)["reason"] as? String, !reason.isEmpty { return "\(name) · \(reason)" }
        if backend == "gemini" {
            if value.hasSuffix("@paid") { return "\(name) · 直连付费" }
            let state = (catalog["gemini_status"] as? [String: [String: Any]])?[value] ?? [:]
            if state["paid_only"] as? Bool == true { return "\(name) · 仅付费" }
            if state["free"] as? Bool == false { return "\(name) · \(state["reason"] as? String ?? "免费档不可用")" }
        }
        return name
    }

    var body: some View {
        Form {
            if let error = model.error { Text(error).foregroundStyle(.red) }
            Section {
                Picker("服务", selection: $backend) {
                    ForEach(backends, id: \.self) { value in
                        Text(["claude": "Claude", "gemini": "Gemini", "codex": "Codex"][value] ?? value)
                            .tag(value).disabled(locked.contains(value))
                    }
                }
                Picker("模型", selection: $variant) {
                    ForEach(variants, id: \.self) { value in Text(modelLabel(value)).tag(value).disabled(!selectable(value)) }
                }
                Picker("思考强度", selection: $depth) {
                    ForEach(depths, id: \.self) { value in Text(value == "auto" ? "自动" : value).tag(value) }
                }
                Toggle("Fast", isOn: $fast).disabled(!fastSupported)
            }
            Section {
                let defaults = info["default"] as? [String: Any] ?? [:]
                Text("默认：\(defaults["variant"] as? String ?? "") · \(defaults["depth"] as? String ?? "")")
                    .font(.caption).foregroundStyle(.secondary)
                Button("恢复此任务默认设置") {
                    Task {
                        let reset: [String: Any] = ["action": action, "backend": "", "variant": "", "depth": "", "fast": false]
                        if await model.save(section: "models", values: ["value": reset]) { dismiss() }
                    }
                }.disabled(model.saving)
            }
        }
        .navigationTitle(label).navigationBarTitleDisplayMode(.inline)
        .onChange(of: backend) { _, _ in
            variant = ((catalog["variants"] as? [String: [String]])?[backend] ?? []).first(where: selectable) ?? ""
            depth = depths.first ?? ""
            if !fastSupported { fast = false }
        }
        .onChange(of: variant) { _, _ in
            if !depths.contains(depth) { depth = depths.first ?? "" }
            if !fastSupported { fast = false }
        }
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("保存") {
                    Task {
                        let value: [String: Any] = ["action": action, "backend": backend, "variant": variant, "depth": depth, "fast": fastSupported && fast]
                        if await model.save(section: "models", values: ["value": value]) { dismiss() }
                    }
                }.disabled(model.saving || !selectable(variant) || !depths.contains(depth) || locked.contains(backend))
            }
        }
    }
}
