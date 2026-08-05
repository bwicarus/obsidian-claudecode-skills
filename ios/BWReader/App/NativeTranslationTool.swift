import SwiftUI
import Translation

struct NativeTranslationToolView: View {
    let initialText: String

    var body: some View {
        if #available(iOS 18.0, *) {
            NativeSystemTranslationView(initialText: initialText)
        } else {
            ContentUnavailableView(
                "需要 iPadOS 18",
                systemImage: "translate",
                description: Text(
                    "系统翻译框架在 iPadOS 18 及以上可用；当前系统仍可继续使用阅读器原有的在线翻译。"
                )
            )
        }
    }
}

@available(iOS 18.0, *)
private struct NativeSystemTranslationView: View {
    private enum TargetLanguage: String, CaseIterable, Identifiable {
        case simplifiedChinese = "zh-Hans"
        case english = "en"
        case japanese = "ja"

        var id: String { rawValue }

        var title: String {
            switch self {
            case .simplifiedChinese:
                return "简体中文"
            case .english:
                return "英语"
            case .japanese:
                return "日语"
            }
        }
    }

    @Environment(\.dismiss) private var dismiss
    @State private var sourceText: String
    @State private var translatedText = ""
    @State private var targetLanguage = TargetLanguage.simplifiedChinese
    @State private var configuration: TranslationSession.Configuration?
    @State private var isTranslating = false
    @State private var errorMessage: String?

    init(initialText: String) {
        _sourceText = State(initialValue: initialText)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("原文（自动识别语言）") {
                    TextEditor(text: $sourceText)
                        .frame(minHeight: 140)
                }

                Section("目标语言") {
                    Picker("翻译为", selection: $targetLanguage) {
                        ForEach(TargetLanguage.allCases) { language in
                            Text(language.title).tag(language)
                        }
                    }
                    .pickerStyle(.segmented)
                }

                Section("系统翻译") {
                    if translatedText.isEmpty {
                        Text("翻译结果会显示在这里")
                            .foregroundStyle(.secondary)
                    } else {
                        Text(translatedText)
                            .textSelection(.enabled)
                    }
                    if let errorMessage {
                        Text(errorMessage)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("系统翻译")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("完成") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isTranslating ? "翻译中…" : "翻译") {
                        requestTranslation()
                    }
                    .disabled(
                        isTranslating ||
                        sourceText.trimmingCharacters(
                            in: .whitespacesAndNewlines
                        ).isEmpty
                    )
                }
            }
            .onChange(of: targetLanguage) { _, _ in
                configuration = nil
                translatedText = ""
            }
            .translationTask(configuration) { session in
                let text = sourceText.trimmingCharacters(
                    in: .whitespacesAndNewlines
                )
                guard !text.isEmpty else { return }
                isTranslating = true
                errorMessage = nil
                defer { isTranslating = false }
                do {
                    let response = try await session.translate(text)
                    translatedText = response.targetText
                } catch {
                    errorMessage = error.localizedDescription
                }
            }
        }
    }

    private func requestTranslation() {
        errorMessage = nil
        if configuration == nil {
            configuration = TranslationSession.Configuration(
                source: nil,
                target: Locale.Language(identifier: targetLanguage.rawValue)
            )
        } else {
            configuration?.invalidate()
        }
    }
}
