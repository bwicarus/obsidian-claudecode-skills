import Combine
import SwiftUI
import Translation

enum ReaderDictionaryTranslationError: LocalizedError {
    case busy
    case empty
    case tooLarge
    case timedOut

    var code: String {
        switch self {
        case .busy:
            return "BW_NATIVE_DICTIONARY_TRANSLATION_BUSY"
        case .empty:
            return "BW_NATIVE_DICTIONARY_TRANSLATION_EMPTY"
        case .tooLarge:
            return "BW_NATIVE_DICTIONARY_TRANSLATION_TOO_LARGE"
        case .timedOut:
            return "BW_NATIVE_DICTIONARY_TRANSLATION_TIMEOUT"
        }
    }

    var errorDescription: String? {
        switch self {
        case .busy:
            return "上一条中文释义仍在生成，请稍后再试"
        case .empty:
            return "没有可翻译的英文词义"
        case .tooLarge:
            return "待翻译词义过长"
        case .timedOut:
            return "系统中文翻译等待超时"
        }
    }
}

@available(iOS 18.0, *)
@MainActor
final class ReaderDictionaryTranslationBroker: ObservableObject {
    static let shared = ReaderDictionaryTranslationBroker()

    private struct ActiveRequest {
        let id: UUID
        let text: String
        let continuation: CheckedContinuation<String, Error>
    }

    @Published fileprivate var configuration: TranslationSession.Configuration?
    private var activeRequest: ActiveRequest?
    private var timeoutTask: Task<Void, Never>?

    private init() {}

    func translateEnglishGloss(_ value: String) async throws -> String {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            throw ReaderDictionaryTranslationError.empty
        }
        guard text.utf8.count <= 2_048 else {
            throw ReaderDictionaryTranslationError.tooLarge
        }
        guard activeRequest == nil else {
            throw ReaderDictionaryTranslationError.busy
        }

        return try await withCheckedThrowingContinuation { continuation in
            let id = UUID()
            activeRequest = ActiveRequest(
                id: id,
                text: text,
                continuation: continuation
            )
            configuration = TranslationSession.Configuration(
                source: Locale.Language(identifier: "en"),
                target: Locale.Language(identifier: "zh-Hans")
            )
            timeoutTask = Task { @MainActor [weak self] in
                do {
                    try await Task.sleep(nanoseconds: 25_000_000_000)
                } catch {
                    return
                }
                self?.finish(
                    id: id,
                    result: .failure(
                        ReaderDictionaryTranslationError.timedOut
                    )
                )
            }
        }
    }

    fileprivate func perform(using session: TranslationSession) async {
        guard let request = activeRequest else { return }
        do {
            let response = try await session.translate(request.text)
            let translated = response.targetText.trimmingCharacters(
                in: .whitespacesAndNewlines
            )
            guard !translated.isEmpty else {
                throw ReaderDictionaryTranslationError.empty
            }
            finish(id: request.id, result: .success(translated))
        } catch {
            finish(id: request.id, result: .failure(error))
        }
    }

    private func finish(id: UUID, result: Result<String, Error>) {
        guard let request = activeRequest, request.id == id else { return }
        activeRequest = nil
        timeoutTask?.cancel()
        timeoutTask = nil
        configuration = nil
        request.continuation.resume(with: result)
    }
}

/// Keeps Apple's Translation task alive without adding another visible Reader
/// surface. The downloaded JMdict remains App-private; only its short English
/// gloss is passed to the system framework when the user opens a word popup.
struct ReaderDictionaryTranslationHost: View {
    var body: some View {
        if #available(iOS 18.0, *) {
            ReaderDictionaryTranslationWorker()
        } else {
            Color.clear
        }
    }
}

@available(iOS 18.0, *)
private struct ReaderDictionaryTranslationWorker: View {
    @ObservedObject private var broker =
        ReaderDictionaryTranslationBroker.shared

    var body: some View {
        Color.clear
            .translationTask(broker.configuration) { session in
                await broker.perform(using: session)
            }
    }
}

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
