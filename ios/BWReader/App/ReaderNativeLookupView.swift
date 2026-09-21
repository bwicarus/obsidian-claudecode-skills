import AVFoundation
import SwiftUI

/// 原生阅读区的查词 / 整段翻译结果面板。
///
/// ⚠ **只负责显示**。查什么词典、走哪个端点、日语还是英语，全在阅读器自己的
/// `window.__bwReaderLookupData` 里（reader.src/15-phrase-wordpop.js），这里不复制
/// 那套判据 —— 它依赖 BOOK_LANGS（这本书声明了哪些语言），复制过来就会变成两份
/// 各自漂移的规则，表现是「同一个词在网页上查中日词典、在原生上查英文词典」。
@MainActor
final class ReaderNativeLookupModel: ObservableObject, Identifiable {
    let id = UUID()
    let text: String
    let mode: String            // "dict" | "translate"
    private let request: ([String: Any]) async -> [String: Any]

    @Published private(set) var loading = true
    @Published private(set) var error: String?
    @Published private(set) var value: [String: Any] = [:]

    init(text: String, mode: String, page: Int, context: String,
         request: @escaping ([String: Any]) async -> [String: Any]) {
        self.text = text
        self.mode = mode
        self.request = request
        self.page = page
        self.context = context
    }

    private let page: Int
    private let context: String

    var title: String { mode == "translate" ? "翻译" : "词典" }

    func load() async {
        loading = true
        error = nil
        defer { loading = false }
        let receipt = await request([
            "action": "nativeSelectionLookup",
            "value": ["text": text, "mode": mode, "page": page, "context": context],
        ])
        guard receipt["ok"] as? Bool == true, let body = receipt["value"] as? [String: Any] else {
            // 出声要带上原因：查不到和查不通是两回事，前者换个词就行，后者要看链路。
            error = receipt["error"] as? String ?? "查询失败，请重试。"
            return
        }
        value = body
    }

    /// 标记掌握 —— 判据（日/英分流）和副作用（重画下划线）都在阅读器那侧，
    /// 这里只发起。成功后本页的生词下划线会跟着消失。
    @Published private(set) var mastered = false
    @Published private(set) var marking = false

    func markMastered() async {
        guard !marking else { return }
        marking = true
        defer { marking = false }
        let receipt = await request([
            "action": "nativeVocabMark",
            "value": ["word": lemma.isEmpty ? headword : lemma,
                      "jp": isJapanese, "mastered": !mastered],
        ])
        guard receipt["ok"] as? Bool == true else {
            error = receipt["error"] as? String ?? "标记失败，请重试。"
            return
        }
        mastered = (receipt["value"] as? [String: Any])?["mastered"] as? Bool ?? !mastered
        onMarked?()
    }

    /// 标完要让原生正文重取一次叠加数据，否则下划线要翻页才消失。
    var onMarked: (() -> Void)?

    /// 发音：日语读假名，英语读词本身。用系统 TTS，不回网页。
    func speak() {
        let spoken = isJapanese && !reading.isEmpty ? reading : headword
        guard !spoken.isEmpty else { return }
        let utterance = AVSpeechUtterance(string: spoken)
        utterance.voice = AVSpeechSynthesisVoice(language: isJapanese ? "ja-JP" : "en-US")
        ReaderNativeSpeech.shared.speak(utterance)
    }

    private func string(_ key: String) -> String { value[key] as? String ?? "" }

    var isJapanese: Bool { value["jp"] as? Bool == true }
    var headword: String { string("word").isEmpty ? text : string("word") }
    var reading: String { string("reading") }
    var phonetic: String { string("phonetic") }
    var lemma: String { string("lemma") }
    var chinese: String { string("zh").isEmpty ? string("translation") : string("zh") }
    var definition: String { string("definition") }
    var kanji: [String] { (value["kanji"] as? [Any] ?? []).compactMap { $0 as? String } }
}

struct ReaderNativeLookupView: View {
    @ObservedObject var model: ReaderNativeLookupModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if model.loading {
                        ProgressView().frame(maxWidth: .infinity, alignment: .center).padding(.top, 24)
                    } else if let error = model.error {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    } else if model.mode == "translate" {
                        Text(model.text).font(.callout).foregroundStyle(ReaderNativeTheme.muted)
                        Divider()
                        Text(model.chinese.isEmpty ? "没有返回译文。" : model.chinese)
                            .font(.body).textSelection(.enabled)
                    } else {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(model.headword).font(.title2.weight(.semibold))
                            Button {
                                model.speak()
                            } label: {
                                Image(systemName: "speaker.wave.2")
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("发音")
                            if !model.reading.isEmpty {
                                Text(model.reading).font(.callout).foregroundStyle(ReaderNativeTheme.muted)
                            }
                            if !model.phonetic.isEmpty {
                                Text(model.phonetic).font(.callout).foregroundStyle(ReaderNativeTheme.muted)
                            }
                        }
                        if !model.lemma.isEmpty, model.lemma != model.headword {
                            Text("原形 " + model.lemma).font(.footnote)
                                .foregroundStyle(ReaderNativeTheme.muted)
                        }
                        if !model.chinese.isEmpty {
                            Text(model.chinese).font(.body).textSelection(.enabled)
                        }
                        if !model.kanji.isEmpty {
                            // 日语汉字拆解：网页那侧也是这么列的。
                            Text(model.kanji.joined(separator: "　")).font(.callout)
                                .foregroundStyle(ReaderNativeTheme.muted)
                        }
                        if !model.definition.isEmpty {
                            Divider()
                            Text(model.definition).font(.callout).textSelection(.enabled)
                        }
                        if model.chinese.isEmpty && model.definition.isEmpty {
                            Text("词典里没有这个词。").foregroundStyle(ReaderNativeTheme.muted)
                        }
                        Divider()
                        Button {
                            Task { await model.markMastered() }
                        } label: {
                            Label(model.mastered ? "已掌握（点此取消）" : "标记掌握",
                                  systemImage: model.mastered ? "checkmark.circle.fill" : "star")
                        }
                        .buttonStyle(.borderless)
                        .disabled(model.marking)
                        .accessibilityHint("标记掌握后这个词不再画生词下划线")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
            }
            .scrollContentBackground(.hidden)
            .background(ReaderNativeTheme.canvas)
            .navigationTitle(model.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task { await model.load() }
        }
        .tint(ReaderNativeTheme.accent)
        .presentationDetents([.medium, .large])
    }
}

/// 全局共用一个合成器。
/// ⚠ 每次新建 `AVSpeechSynthesizer` 会让上一句**还没念完就被回收**，表现是
/// "点了发音没声音"或只响半个音。系统要求它活到念完为止。
@MainActor
final class ReaderNativeSpeech {
    static let shared = ReaderNativeSpeech()
    private let synthesizer = AVSpeechSynthesizer()
    private init() {}

    func speak(_ utterance: AVSpeechUtterance) {
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
        synthesizer.speak(utterance)
    }
}
