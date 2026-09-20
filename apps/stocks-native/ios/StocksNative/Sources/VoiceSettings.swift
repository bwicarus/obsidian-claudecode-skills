import SwiftUI

struct VoiceSettingsValues: Codable, Equatable {
    var backendModel: String
    var effort: String
    var voice: String
}

struct VoiceSettingsResponse: Decodable {
    let settings: VoiceSettingsValues
    let applies: String
}

struct VoiceSettingsCatalog: Decodable {
    struct Model: Decodable, Identifiable {
        let id: String
        let displayName: String?
        let defaultEffort: String?
        let efforts: [String]
        var label: String { displayName.flatMap { $0.isEmpty ? nil : $0 } ?? id }
    }

    struct Voice: Decodable, Identifiable {
        let id: String
        let displayName: String?
        var label: String { displayName.flatMap { $0.isEmpty ? nil : $0 } ?? id }
    }

    let models: [Model]
    let voices: [Voice]
    let defaults: VoiceSettingsValues
}

struct VoiceSettingsView: View {
    @ObservedObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var catalog: VoiceSettingsCatalog?
    @State private var saved: VoiceSettingsValues?
    @State private var draft: VoiceSettingsValues?
    @State private var loadedScope: String?
    @State private var generation = UUID()
    @State private var isLoading = false
    @State private var isSaving = false
    @State private var loadError: String?
    @State private var saveError: String?
    @State private var didSave = false
    @State private var saveTask: Task<Void, Never>?

    private var scope: String {
        StockSelectionModel.scopeID(client: model.isPaired && model.isAIEnabled ? model.client : nil)
    }

    private var selectedModel: VoiceSettingsCatalog.Model? {
        catalog?.models.first { $0.id == draft?.backendModel }
    }

    private var validSelection: Bool {
        guard let catalog, let draft, let selectedModel else { return false }
        return catalog.voices.contains { $0.id == draft.voice } &&
            (selectedModel.efforts.contains(draft.effort) ||
             (selectedModel.efforts.isEmpty && draft.effort.isEmpty))
    }

    private var canSave: Bool {
        loadedScope == scope && scope != "signed-out" && !isLoading && !isSaving &&
            loadError == nil && draft != saved && validSelection
    }

    var body: some View {
        NavigationStack {
            Form {
                if scope == "signed-out" {
                    Text("登录可使用 AI 的账户后设置语音。")
                        .foregroundStyle(.secondary)
                } else if isLoading || loadedScope != scope {
                    ProgressView("正在读取语音设置…")
                } else if let loadError {
                    Section {
                        Text(loadError).foregroundStyle(.red)
                        Button("重试") { Task { await load() } }
                    }
                } else if let catalog, let draft {
                    Section {
                        Picker("分析模型", selection: binding(\.backendModel)) {
                            if selectedModel == nil {
                                Text("当前模型已不可用").tag(draft.backendModel)
                            }
                            ForEach(catalog.models) { item in
                                Text(item.label).tag(item.id)
                            }
                        }
                        if let selectedModel, !selectedModel.efforts.isEmpty {
                            Picker("思考强度", selection: binding(\.effort)) {
                                if !selectedModel.efforts.contains(draft.effort) {
                                    Text("请选择").tag(draft.effort)
                                }
                                ForEach(selectedModel.efforts, id: \.self) { effort in
                                    Text(effortLabel(effort)).tag(effort)
                                }
                            }
                        }
                        Picker("声音", selection: binding(\.voice)) {
                            if !catalog.voices.contains(where: { $0.id == draft.voice }) {
                                Text("当前声音已不可用").tag(draft.voice)
                            }
                            ForEach(catalog.voices) { item in
                                Text(item.label).tag(item.id)
                            }
                        }
                    } footer: {
                        Text("分析模型负责查询和分析股票，声音用于语音通话。保存后下次连接生效，原对话会保留，当前通话不会中断。")
                    }
                    .pickerStyle(.menu)
                    .disabled(isSaving)

                    if !validSelection {
                        Text("部分设置已不在服务器提供的选项中，请重新选择。")
                            .font(.footnote).foregroundStyle(.orange)
                    }
                    if let saveError {
                        Text(saveError).foregroundStyle(.red)
                    }
                    if didSave {
                        Label("已保存，下次连接生效", systemImage: "checkmark.circle")
                            .foregroundStyle(AppStyle.accent)
                    }
                }
            }
            .navigationTitle("语音设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("完成") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        saveTask = Task { await save() }
                    } label: {
                        if isSaving { ProgressView() } else { Text("保存") }
                    }
                    .disabled(!canSave)
                }
            }
            .tint(AppStyle.accent)
            .task(id: scope) { await load() }
            .onDisappear {
                generation = UUID()
                saveTask?.cancel()
            }
        }
    }

    private func binding(_ key: WritableKeyPath<VoiceSettingsValues, String>) -> Binding<String> {
        Binding(get: { draft?[keyPath: key] ?? "" }, set: { value in
            guard var next = draft else { return }
            next[keyPath: key] = value
            if key == \VoiceSettingsValues.backendModel,
               let selected = catalog?.models.first(where: { $0.id == value }),
               !selected.efforts.contains(next.effort) {
                next.effort = selected.defaultEffort.flatMap {
                    selected.efforts.contains($0) ? $0 : nil
                } ?? selected.efforts.first ?? ""
            }
            draft = next
            didSave = false
            saveError = nil
        })
    }

    @MainActor private func load() async {
        saveTask?.cancel()
        let ticket = UUID(), requestedScope = scope
        generation = ticket
        loadedScope = nil
        catalog = nil; saved = nil; draft = nil
        loadError = nil; saveError = nil; didSave = false
        isSaving = false
        guard requestedScope != "signed-out" else { isLoading = false; return }
        isLoading = true
        let client = model.client
        defer { if generation == ticket { isLoading = false } }
        do {
            async let options = client.voiceCatalog()
            async let settings = client.voiceSettings()
            let (receivedCatalog, response) = try await (options, settings)
            guard !Task.isCancelled, generation == ticket, scope == requestedScope else { return }
            guard !receivedCatalog.models.isEmpty, !receivedCatalog.voices.isEmpty else {
                throw AppError.message("服务器尚未提供可用的模型或声音，请稍后重试。")
            }
            guard response.applies == "next_connection" else {
                throw AppError.message("服务器的设置生效方式暂不受支持，请更新后重试。")
            }
            catalog = receivedCatalog
            saved = response.settings
            var selection = response.settings
            if let selected = receivedCatalog.models.first(where: { $0.id == selection.backendModel }),
               selected.efforts.isEmpty { selection.effort = "" }
            draft = selection
            loadedScope = requestedScope
        } catch {
            guard !Task.isCancelled, generation == ticket, scope == requestedScope else { return }
            loadedScope = requestedScope
            loadError = error.localizedDescription
        }
    }

    @MainActor private func save() async {
        guard canSave, let requested = draft else { return }
        let ticket = generation, requestedScope = scope, client = model.client
        isSaving = true; saveError = nil; didSave = false
        defer { if generation == ticket { isSaving = false } }
        do {
            let response = try await client.saveVoiceSettings(requested)
            guard !Task.isCancelled, generation == ticket, scope == requestedScope else { return }
            guard response.applies == "next_connection" else {
                throw AppError.message("设置已提交，但无法确认生效方式，请重新打开设置核对。")
            }
            saved = response.settings
            draft = response.settings
            didSave = true
        } catch {
            guard !Task.isCancelled, generation == ticket, scope == requestedScope else { return }
            saveError = error.localizedDescription
        }
    }

    private func effortLabel(_ value: String) -> String {
        switch value {
        case "none": return "不额外思考"
        case "minimal": return "最少"
        case "low": return "低"
        case "medium": return "中"
        case "high": return "高"
        case "xhigh": return "很高"
        case "max": return "最大"
        case "ultra": return "最高"
        default: return value
        }
    }
}
