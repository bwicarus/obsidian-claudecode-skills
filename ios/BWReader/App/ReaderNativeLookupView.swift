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
