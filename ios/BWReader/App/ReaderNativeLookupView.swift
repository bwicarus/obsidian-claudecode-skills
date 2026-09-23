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

    var title: String {
        switch mode {
        case "phrase": return "词组"
        case "translate": return "翻译"
        case "explain": return "解释"
        default: return "词典"
        }
    }

    /// 解释的正文（Markdown 原文；这里按段显示，不引渲染器）。
    var explanation: [String] {
        string("body").split(separator: "\n", omittingEmptySubsequences: false)
            .map { line -> String in
                var text = line.trimmingCharacters(in: .whitespaces)
                while let first = text.first, "#-*>".contains(first) {
                    text.removeFirst()
                    text = text.trimmingCharacters(in: .whitespaces)
                }
                return text
            }
            .filter { !$0.isEmpty }
    }

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
        favorited = body["fav"] as? Bool == true
        mastered = body["mastered"] as? Bool == true
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

    /// 「展开完整词典」—— 同一条端点的一次性 JSON（网页小框那边点展开走它的 SSE 版）。
    /// 拿到后就地把 value 换成完整那份：headword/释义这些键名两版一致，所以视图里
    /// 已有的部分不用改，只是多出例句/同反义。
    @Published private(set) var expanded = false
    @Published private(set) var expanding = false

    func expand() async {
        guard !expanding, !expanded else { return }
        expanding = true
        defer { expanding = false }
        let receipt = await request([
            "action": "nativeSelectionLookup",
            "value": ["text": lemma.isEmpty ? headword : lemma, "mode": "dict-full",
                      "page": page, "context": context],
        ])
        guard receipt["ok"] as? Bool == true, let body = receipt["value"] as? [String: Any] else {
            error = receipt["error"] as? String ?? "展开失败，请重试。"
            return
        }
        // ⚠ 合并而不是替换：完整那份没有 mastered / reading / kanji 这些小框才有的键，
        // 直接换掉会让「已掌握」按钮和日语读音在展开后凭空消失。
        var merged = value
        for (key, item) in body where !(item is NSNull) { merged[key] = item }
        value = merged
        expanded = true
    }

    /// 词组：收藏起来当一个分词单元。⚠ 本地先翻、真分词重算、长下划线即时画、
    /// outbox 兜底，四件事都挂在底座 `_phraseFav` 上 —— 这里只发起，状态以回执为准。
    @Published private(set) var favorited = false
    @Published private(set) var favoriting = false

    func toggleFavorite() async {
        guard !favoriting else { return }
        favoriting = true
        defer { favoriting = false }
        let receipt = await request([
            "action": "nativePhraseFav",
            "value": ["text": headword],
        ])
        guard receipt["ok"] as? Bool == true else {
            error = receipt["error"] as? String ?? "收藏失败，请重试。"
            return
        }
        favorited = (receipt["value"] as? [String: Any])?["fav"] as? Bool ?? !favorited
        onMarked?()
    }

    var isPhrase: Bool { mode == "phrase" }

    private func string(_ key: String) -> String { value[key] as? String ?? "" }

    var isJapanese: Bool { value["jp"] as? Bool == true }
    var headword: String { string("word").isEmpty ? text : string("word") }
    var reading: String { string("reading") }
    var phonetic: String { string("phonetic") }
    var lemma: String { string("lemma") }
    var chinese: String { string("zh").isEmpty ? string("translation") : string("zh") }
    var definition: String { string("definition") }
    var kanji: [String] { (value["kanji"] as? [Any] ?? []).compactMap { $0 as? String } }

    /// 例句：英语完整词条给的是字符串数组，日语小框给的是 {ja, zh} 对象数组。
    /// 两种都收进来，显示时一视同仁 —— 分成两个字段会让视图里多一处分叉。
    var examples: [(String, String)] {
        (value["examples"] as? [Any] ?? []).compactMap { item in
            if let text = item as? String { return (text, "") }
            guard let pair = item as? [String: Any] else { return nil }
            let source = pair["ja"] as? String ?? pair["en"] as? String ?? ""
            guard !source.isEmpty else { return nil }
            return (source, pair["zh"] as? String ?? "")
        }
    }
    /// 原版单词小框里的几样：词性小标签、变形/语法标签行、BNC 词频。
    var partOfSpeech: String { string("pos") }
    var inflection: String { string("inflect") }
    var frequency: Int { (value["freq"] as? NSNumber)?.intValue ?? 0 }

    /// 「语法」（原版小框的 `_wordPopGrammar`）：对这个词所在的整句做语法分析，焦点是这个词。
    var onGrammar: ((String, String) -> Void)?
    func grammar() { onGrammar?(context.isEmpty ? headword : context, headword) }

    var synonyms: [String] { (value["synonyms"] as? [Any] ?? []).compactMap { $0 as? String } }
    var antonyms: [String] { (value["antonyms"] as? [Any] ?? []).compactMap { $0 as? String } }
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
                    } else if model.isPhrase {
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(model.headword).font(.title3.weight(.semibold))
                            if !model.reading.isEmpty {
                                Text(model.reading).font(.callout)
                                    .foregroundStyle(ReaderNativeTheme.muted)
                            }
                            Button { model.speak() } label: { Image(systemName: "speaker.wave.2") }
                                .buttonStyle(.plain)
                                .accessibilityLabel("发音")
                        }
                        Text(model.chinese.isEmpty ? "（无翻译）" : model.chinese)
                            .font(.body).textSelection(.enabled)
                        if !model.kanji.isEmpty {
                            Text(model.kanji.joined(separator: "　")).font(.callout)
                                .foregroundStyle(ReaderNativeTheme.muted)
                        }
                        Divider()
                        Button {
                            Task { await model.toggleFavorite() }
                        } label: {
                            Label(model.favorited ? "已收藏（点此取消）" : "收藏为词组",
                                  systemImage: model.favorited ? "star.fill" : "star")
                        }
                        .buttonStyle(.borderless)
                        .disabled(model.favoriting)
                        .accessibilityHint("收藏后这几个字之后会当作一个词来分词")
                        Button {
                            Task { await model.markMastered() }
                        } label: {
                            Label(model.mastered ? "已掌握（点此取消）" : "标记掌握",
                                  systemImage: model.mastered ? "checkmark.circle.fill" : "star")
                        }
                        .buttonStyle(.borderless)
                        .disabled(model.marking)
                    } else if model.mode == "explain" {
                        Text(model.text).font(.callout).foregroundStyle(ReaderNativeTheme.muted)
                        Divider()
                        if model.explanation.isEmpty {
                            Text("没有返回解释。").foregroundStyle(ReaderNativeTheme.muted)
                        } else {
                            ForEach(Array(model.explanation.enumerated()), id: \.offset) { _, line in
                                Text(line).font(.callout).textSelection(.enabled)
                            }
                        }
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
                            if model.frequency > 0 {
                                Text("BNC#\(model.frequency)").font(.caption2.monospacedDigit())
                                    .foregroundStyle(ReaderNativeTheme.muted)
                            }
                        }
                        if !model.inflection.isEmpty {
                            // 变形：日语=原形 + 语法标签（过去た/否定ない/て形…）；英语=各种屈折变形。
                            Text(model.inflection).font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                        }
                        if !model.lemma.isEmpty, model.lemma != model.headword {
                            Text("原形 " + model.lemma).font(.footnote)
                                .foregroundStyle(ReaderNativeTheme.muted)
                        }
                        if !model.chinese.isEmpty {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                if !model.partOfSpeech.isEmpty {
                                    // 词性单独做暗色小标签（原版 .wp-pos-tag）。
                                    Text(model.partOfSpeech).font(.caption2)
                                        .padding(.horizontal, 5).padding(.vertical, 1)
                                        .background(ReaderNativeTheme.card, in: RoundedRectangle(cornerRadius: 4))
                                        .foregroundStyle(ReaderNativeTheme.muted)
                                }
                                Text(model.chinese).font(.body).textSelection(.enabled)
                            }
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
                        if !model.examples.isEmpty {
                            Divider()
                            VStack(alignment: .leading, spacing: 8) {
                                ForEach(Array(model.examples.enumerated()), id: \.offset) { _, pair in
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(pair.0).font(.callout)
                                        if !pair.1.isEmpty {
                                            Text(pair.1).font(.footnote)
                                                .foregroundStyle(ReaderNativeTheme.muted)
                                        }
                                    }
                                }
                            }
                            .textSelection(.enabled)
                        }
                        if !model.synonyms.isEmpty || !model.antonyms.isEmpty {
                            let parts = [model.synonyms.isEmpty ? "" : "同 " + model.synonyms.prefix(5).joined(separator: ", "),
                                         model.antonyms.isEmpty ? "" : "反 " + model.antonyms.prefix(5).joined(separator: ", ")]
                            Text(parts.filter { !$0.isEmpty }.joined(separator: " · "))
                                .font(.footnote).foregroundStyle(ReaderNativeTheme.muted)
                        }
                        // 日语不出这个按钮：日语的「展开」在网页上是另一条路（离线富内容
                        // 小框已经给了 + 按需的 AI 深入讲解），不是同一个端点。
                        if !model.expanded, !model.isJapanese {
                            Button {
                                Task { await model.expand() }
                            } label: {
                                Label(model.expanding ? "展开中…" : "展开完整词典",
                                      systemImage: "chevron.down.circle")
                            }
                            .buttonStyle(.borderless)
                            .disabled(model.expanding)
                        }
                        Divider()
                        // 原版小框底部那一排：标记掌握 + 语法。
                        HStack(spacing: 10) {
                            Button {
                                Task { await model.markMastered() }
                            } label: {
                                Label(model.mastered ? "已掌握 100" : "标记掌握",
                                      systemImage: model.mastered ? "checkmark.circle.fill" : "star")
                            }
                            .buttonStyle(.bordered)
                            .tint(model.mastered ? .green : nil)
                            .disabled(model.marking)
                            .accessibilityHint("标记掌握后这个词不再画生词下划线；再点取消")
                            Button { model.grammar() } label: {
                                Label("语法", systemImage: "chart.bar.doc.horizontal")
                            }
                            .buttonStyle(.bordered)
                            .accessibilityHint("对这个词所在的整句做语法分析")
                        }
                        .font(.footnote)
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
