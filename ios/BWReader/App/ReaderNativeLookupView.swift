import AVFoundation
import SwiftUI

/// 原生阅读区的查词 / 整段翻译结果面板。
///
/// 此模型负责显示，数据由宿主路由：翻译、例句中译和完整英语词典直接走 Swift
/// 网关；日语词条、改进和其他尚未迁移的动作仍走共享语义接口。
/// 语言判断读取当前书声明的语言，迁移时须保留原字段和后备行为。
///
/// 2026-09-24 用户："词典内容也和之前不一样，少了很多元素 …… 应该在旧的基础上改动，
/// 进行一定美化而不是现在这样的阉割"。所以这里逐项对齐原版 `#word-pop` 与完整字典框：
/// 音调线、变形/源词行、释义来源、母语例句（缺中文就地补）、汉字音训、词锚卡、
/// 「AI 深度解释」、标记掌握 / Anki / 语法。
@MainActor
final class ReaderNativeLookupModel: ObservableObject, Identifiable {
    let id = UUID()
    let text: String
    let mode: String            // "dict" | "phrase" | "translate" | "explain"
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
    var explanation: [String] { Self.paragraphs(string("body")) }

    static func paragraphs(_ body: String) -> [String] {
        body.split(separator: "\n", omittingEmptySubsequences: false)
            .map { line -> String in
                var text = line.trimmingCharacters(in: .whitespaces)
                while let first = text.first, "#-*>".contains(first) {
                    text.removeFirst()
                    text = text.trimmingCharacters(in: .whitespaces)
                }
                return text.replacingOccurrences(of: "**", with: "")
            }
            .filter { !$0.isEmpty }
    }

    private func lookup(_ mode: String, _ text: String, context: String? = nil) async -> [String: Any] {
        await request([
            "action": "nativeSelectionLookup",
            "value": ["text": text, "mode": mode, "page": page, "context": context ?? self.context],
        ])
    }

    /// 只查一次：阅读器可能先在后台开查（等不等得到决定弹不弹小框），小框出现时不再重查。
    private var loadStarted = false

    func load() async {
        guard !loadStarted else { return }
        loadStarted = true
        loading = true
        error = nil
        let receipt = await lookup(mode, text)
        guard receipt["ok"] as? Bool == true, let body = receipt["value"] as? [String: Any] else {
            let reason = receipt["error"] as? String ?? "查询失败，请重试。"
            // 英语 ecdict 没有这个词：原版直接开三源完整字典，这里同样接着展开。
            if mode == "dict", reason.contains("LOOKUP_MISS"), text.allSatisfy({ $0.isASCII }) {
                await expand()
                if expanded { loading = false; return }
            }
            // 出声要带上原因：查不到和查不通是两回事，前者换个词就行，后者要看链路。
            error = reason
            loading = false
            return
        }
        value = body
        favorited = body["fav"] as? Bool == true
        mastered = body["mastered"] as? Bool == true
        exampleZh = [:]
        selectedKanji = 0
        loading = false
        // 小框出来之后的那几样后台补：母语例句的中文、这个词绑着的卡。
        guard mode == "dict" || mode == "phrase" else { return }
        Task { await fillExampleZh() }
        Task { await loadCards() }
    }

    // MARK: 标记掌握

    /// 原生词汇命令确认服务器标记并提交本机词汇仓，成功后刷新当前页下划线。
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

    // MARK: 展开（英语三源完整词条）

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

    // MARK: 词组收藏

    /// Swift 保存明确的收藏状态；分词和镜像由持久化队列更新。
    @Published private(set) var favorited = false
    @Published private(set) var favoriting = false

    func toggleFavorite() async {
        guard !favoriting else { return }
        favoriting = true
        defer { favoriting = false }
        let receipt = await request([
            "action": "nativePhraseFav",
            "value": ["text": headword, "enabled": !favorited],
        ])
        guard receipt["ok"] as? Bool == true else {
            error = receipt["error"] as? String ?? "收藏失败，请重试。"
            return
        }
        favorited = (receipt["value"] as? [String: Any])?["fav"] as? Bool ?? !favorited
        onMarked?()
    }

    var isPhrase: Bool { mode == "phrase" }

    // MARK: 母语例句补中文（原版 _fillJapaneseExampleZh：缺中文的句子逐句请译，就地补上）

    @Published private(set) var exampleZh: [Int: String] = [:]
    @Published private(set) var exampleZhPending: Set<Int> = []

    private func fillExampleZh() async {
        guard isJapanese else { return }
        for (index, pair) in examples.enumerated() where pair.1.isEmpty {
            exampleZhPending.insert(index)
            let receipt = await lookup("example-zh", pair.0)
            exampleZhPending.remove(index)
            let zh = ((receipt["value"] as? [String: Any])?["zh"] as? String ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !zh.isEmpty { exampleZh[index] = zh }
        }
    }

    // MARK: 词锚卡（原版小框顶部那段「🔖 卡片」：这个词绑着的卡）

    struct BoundCard: Identifiable { let id: String; let label: String; let text: String }
    @Published private(set) var cards: [BoundCard] = []

    private func loadCards() async {
        let key = lemma.isEmpty ? headword : lemma
        let receipt = await lookup("word-cards", key, context: headword)
        let rows = (receipt["value"] as? [String: Any])?["cards"] as? [[String: Any]] ?? []
        cards = rows.enumerated().compactMap { index, row in
            let text = row["text"] as? String ?? ""
            let label = row["label"] as? String ?? "卡片"
            guard !text.isEmpty || !label.isEmpty else { return nil }
            let cid = row["cid"] as? String ?? ""
            return BoundCard(id: cid.isEmpty ? "card-\(index)" : cid, label: label, text: text)
        }
    }

    // MARK: AI 深度解释（原版完整字典框底部那颗按钮：句境 / 用法 / 语感 / 近义辨析）

    @Published private(set) var aiText: [String] = []
    @Published private(set) var aiLoading = false
    @Published private(set) var aiError: String?

    func deepExplain() async {
        guard !aiLoading else { return }
        aiLoading = true
        aiError = nil
        defer { aiLoading = false }
        let receipt = await lookup("jp-ai", headword)
        guard receipt["ok"] as? Bool == true,
              let body = (receipt["value"] as? [String: Any])?["body"] as? String else {
            aiError = receipt["error"] as? String ?? "AI 深度解释失败，请重试。"
            return
        }
        aiText = Self.paragraphs(body)
    }

    // MARK: 加入 Anki（原版完整字典框「🎴 加入 Anki」）

    @Published private(set) var ankiState: String?
    @Published private(set) var ankiBusy = false

    func addToAnki() async {
        guard !ankiBusy else { return }
        ankiBusy = true
        defer { ankiBusy = false }
        let receipt = await lookup("vocab-anki", lemma.isEmpty ? headword : lemma)
        guard receipt["ok"] as? Bool == true else {
            ankiState = "失败"
            error = receipt["error"] as? String ?? "加入 Anki 失败。"
            return
        }
        let action = (receipt["value"] as? [String: Any])?["action"] as? String
        ankiState = action == "updated" ? "已更新" : "已加入"
    }

    // MARK: 字段

    private func string(_ key: String) -> String { value[key] as? String ?? "" }

    var isJapanese: Bool { value["jp"] as? Bool == true }
    /// 词典里没有这个日语词（合成的兜底词条）：原版同样出标准小框，释义处请 AI 讲。
    var isMissing: Bool { value["missing"] as? Bool == true }
    var headword: String { string("word").isEmpty ? text : string("word") }
    var reading: String { string("reading") }
    var accent: Int? { (value["accent"] as? NSNumber)?.intValue }
    var phonetic: String { string("phonetic") }
    var lemma: String { string("lemma") }
    var chinese: String {
        for key in ["meaning", "zh", "translation"] where !string(key).isEmpty { return string(key) }
        return ""
    }
    var definition: String { string("definition") }
    var meaningSource: String { string("source") }
    /// 外来语源词行（原版 .jp-source：源词 primary health care · 英语 / 和製英語）。
    var origin: String { string("origin") }

    struct Kanji: Identifiable { let id: Int; let literal: String; let on: [String]; let kun: [String]; let meaning: String }
    var kanji: [Kanji] {
        (value["kanji"] as? [Any] ?? []).enumerated().compactMap { index, item in
            if let literal = item as? String { return Kanji(id: index, literal: literal, on: [], kun: [], meaning: "") }
            guard let row = item as? [String: Any], let literal = row["kanji"] as? String, !literal.isEmpty else { return nil }
            return Kanji(id: index, literal: literal,
                         on: row["on"] as? [String] ?? [], kun: row["kun"] as? [String] ?? [],
                         meaning: row["meaning"] as? String ?? row["meanings_zh"] as? String ?? "")
        }
    }
    @Published var selectedKanji = 0

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
                ReaderNativeLookupContent(model: model)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollContentBackground(.hidden)
            .background(WordPopStyle.surface)
            .navigationTitle(model.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } }
            }
            .task { await model.load() }
        }
        .environment(\.colorScheme, .dark)
        .tint(WordPopStyle.accent)
        .presentationDetents([.medium, .large])
    }
}

/// 原版 `#word-pop` 的配色（rc-core 的 --rc-* 令牌 + rc-wordpop 的专用色）。
enum WordPopStyle {
    static let surface = Color(red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255)     // --rc-bg-surface
    static let raised = Color(red: 0x2c / 255, green: 0x2c / 255, blue: 0x2e / 255)      // --rc-bg-raised
    static let border = Color(red: 84 / 255, green: 84 / 255, blue: 88 / 255).opacity(0.62)
    static let accent = Color(red: 10 / 255, green: 132 / 255, blue: 1)                  // --rc-accent
    static let accentBorder = accent.opacity(0.58)
    static let cyan = Color(red: 0x64 / 255, green: 0xd2 / 255, blue: 1)                 // --rc-accent-cyan
    static let success = Color(red: 0x30 / 255, green: 0xd1 / 255, blue: 0x58 / 255)
    static let dim = Color(red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.38)
    static let muted = Color(red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.62)
    static let inflect = Color(red: 0x9f / 255, green: 0xb4 / 255, blue: 0xcf / 255)
    static let exJa = Color(red: 0xdf / 255, green: 0xe9 / 255, blue: 1)
    static let exZh = Color(red: 0x8f / 255, green: 0xb0 / 255, blue: 0xd8 / 255)
    static let source = Color(red: 0x6f / 255, green: 0x7e / 255, blue: 0x96 / 255)
    static let drop = Color(red: 1, green: 0x8a / 255, blue: 0x8a / 255)
    static let origin = Color(red: 1, green: 0xe0 / 255, blue: 0xb2 / 255)
    static let cardTint = Color(red: 140 / 255, green: 120 / 255, blue: 1)
    static let rule = Color(red: 0x24 / 255, green: 0x30 / 255, blue: 0x49 / 255)
}

/// 查词结果的正文。底部面板与贴词小框共用这一份；版式照原版小框：
/// 词头行 → 变形/源词 → 词锚卡 → 释义区（释义、来源、例句、展开）→ 汉字 → AI → 底部按钮条。
struct ReaderNativeLookupContent: View {
    @ObservedObject var model: ReaderNativeLookupModel

    var body: some View {
        Group {
            if model.loading {
                ProgressView().tint(WordPopStyle.muted)
                    .frame(maxWidth: .infinity, alignment: .center).padding(.vertical, 28)
            } else if let error = model.error, model.value.isEmpty {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 13)).foregroundStyle(.orange)
                    .padding(.leading, 14).padding(.trailing, 34).padding(.vertical, 14)
            } else if model.mode == "explain" {
                plainResult(lines: model.explanation, empty: "没有返回解释。")
            } else if model.mode == "translate" || (model.isPhrase && !model.isJapanese) {
                plainResult(lines: model.chinese.isEmpty ? [] : [model.chinese], empty: "没有返回译文。")
            } else {
                dictionary
            }
        }
        .foregroundStyle(.white)
    }

    // MARK: 翻译 / 解释 / 非日语词组

    private func plainResult(lines: [String], empty: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(model.text)
                    .font(.system(size: model.isPhrase ? 17 : 13, weight: model.isPhrase ? .semibold : .regular))
                    .foregroundStyle(model.isPhrase ? Color.white : WordPopStyle.muted)
                    .lineLimit(model.isPhrase ? nil : 4)
                if model.isPhrase { speakButton }
            }
            .padding(.leading, 14).padding(.trailing, 34).padding(.top, 12).padding(.bottom, 8)
            VStack(alignment: .leading, spacing: 8) {
                if lines.isEmpty {
                    Text(empty).foregroundStyle(WordPopStyle.muted)
                } else {
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                        Text(line).font(.system(size: 14)).lineSpacing(4).textSelection(.enabled)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14).padding(.vertical, 10)
            .overlay(alignment: .top) { divider }
            if model.isPhrase {
                actionBar {
                    barButton(model.favorited ? "已收藏" : "收藏为词组",
                              icon: model.favorited ? "star.fill" : "star", on: model.favorited,
                              busy: model.favoriting) { Task { await model.toggleFavorite() } }
                        .accessibilityHint("收藏后这几个字之后会当作一个词来分词")
                    masterButton
                }
            }
        }
    }

    // MARK: 单词（日 / 英）

    private var dictionary: some View {
        VStack(alignment: .leading, spacing: 0) {
            head
            if !model.inflection.isEmpty {
                infoLine(model.inflection, icon: "arrow.triangle.2.circlepath", color: WordPopStyle.inflect)
            } else if !model.lemma.isEmpty, model.lemma != model.headword {
                infoLine("原形 " + model.lemma, icon: "arrow.triangle.2.circlepath", color: WordPopStyle.inflect)
            }
            if !model.origin.isEmpty { infoLine(model.origin, icon: "globe", color: WordPopStyle.origin) }
            if !model.cards.isEmpty { boundCards }
            definitionSection
            if model.isJapanese, !model.kanji.isEmpty { kanjiSection }
            if model.isJapanese { aiSection }
            if let error = model.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 12)).foregroundStyle(.orange)
                    .padding(.horizontal, 14).padding(.bottom, 8)
            }
            actionBar {
                masterButton
                if model.isPhrase {
                    barButton(model.favorited ? "已收藏" : "词组", icon: model.favorited ? "star.fill" : "star",
                              on: model.favorited, busy: model.favoriting) { Task { await model.toggleFavorite() } }
                }
                barButton(model.ankiState.map { "Anki " + $0 } ?? "Anki", icon: "rectangle.stack.badge.plus",
                          on: model.ankiState == "已加入" || model.ankiState == "已更新", busy: model.ankiBusy) {
                    Task { await model.addToAnki() }
                }
                .accessibilityHint("把这个词加入 Anki")
                barButton("语法", icon: "chart.bar.doc.horizontal") { model.grammar() }
                    .accessibilityHint("对这个词所在的整句做语法分析")
            }
        }
    }

    /// 词头行（原版 .wp-head）：词 · 音调线（日）/ 音标（英）· 发音圆钮 · BNC#。
    private var head: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(model.headword).font(.system(size: 19, weight: .semibold)).foregroundStyle(.white)
                .textSelection(.enabled)
            if model.isJapanese, !model.reading.isEmpty {
                if let accent = model.accent {
                    ReaderNativePitchView(reading: model.reading, accent: accent)
                } else {
                    Text(model.reading).font(.system(size: 13)).foregroundStyle(WordPopStyle.muted)
                }
            } else if !model.phonetic.isEmpty {
                Text(model.phonetic).font(.system(size: 12).italic()).foregroundStyle(WordPopStyle.muted)
            }
            speakButton
            Spacer(minLength: 0)
            if model.frequency > 0 {
                Text("BNC#\(model.frequency)").font(.system(size: 11).monospacedDigit())
                    .foregroundStyle(WordPopStyle.dim)
            }
        }
        .padding(.leading, 14).padding(.trailing, 34).padding(.top, 12).padding(.bottom, 7)
    }

    private var speakButton: some View {
        Button { model.speak() } label: {
            Image(systemName: "speaker.wave.2.fill").font(.system(size: 11))
                .foregroundStyle(.white)
                .frame(width: 26, height: 26)
                .overlay(Circle().stroke(WordPopStyle.accentBorder, lineWidth: 1))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("发音")
    }

    /// 变形 / 源词行（原版 .jp-inflect）。
    private func infoLine(_ text: String, icon: String, color: Color) -> some View {
        Label {
            Text(text).font(.system(size: 12)).foregroundStyle(color).lineSpacing(2)
        } icon: {
            Image(systemName: icon).font(.system(size: 10)).foregroundStyle(color.opacity(0.8))
        }
        .padding(.horizontal, 14).padding(.bottom, 6)
    }

    /// 词锚卡（原版 .wp-cards：紫色细框，多张按加入顺序全列）。
    private var boundCards: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(model.cards) { card in
                VStack(alignment: .leading, spacing: 3) {
                    Text("🔖 " + card.label).font(.system(size: 12, weight: .semibold)).opacity(0.92)
                    if !card.text.isEmpty {
                        Text(card.text).font(.system(size: 12)).foregroundStyle(WordPopStyle.muted)
                            .lineLimit(6).textSelection(.enabled)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(WordPopStyle.cardTint.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(WordPopStyle.cardTint.opacity(0.4), lineWidth: 1))
        .padding(.horizontal, 14).padding(.bottom, 8)
    }

    /// 释义区（原版 .wp-def：上边一条分隔线；词性小标签 + 释义 + 来源 + 母语例句）。
    private var definitionSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                if !model.partOfSpeech.isEmpty {
                    Text(model.partOfSpeech).font(.system(size: 10, weight: .medium))
                        .foregroundStyle(WordPopStyle.dim)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(WordPopStyle.raised, in: RoundedRectangle(cornerRadius: 4))
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(WordPopStyle.border, lineWidth: 1))
                }
                if model.isMissing {
                    Text("暂无词典释义（可能是复合词/专有名词）。可以让 AI 结合上下文讲解 ↓")
                        .font(.system(size: 13)).foregroundStyle(WordPopStyle.muted)
                } else if !model.chinese.isEmpty {
                    Text(model.chinese).font(.system(size: model.isJapanese ? 15 : 14)).lineSpacing(4)
                        .textSelection(.enabled)
                } else if model.definition.isEmpty {
                    Text("（无释义）").font(.system(size: 13)).foregroundStyle(WordPopStyle.exZh)
                }
            }
            if !model.definition.isEmpty {
                Text(model.definition).font(.system(size: 13)).lineSpacing(3)
                    .foregroundStyle(WordPopStyle.muted)
                    .lineLimit(model.expanded ? nil : 3)
                    .padding(.top, model.chinese.isEmpty ? 0 : 6)
                    .textSelection(.enabled)
            }
            if !model.meaningSource.isEmpty {
                Text(model.meaningSource).font(.system(size: 10)).foregroundStyle(WordPopStyle.source).padding(.top, 4)
            }
            if !model.examples.isEmpty { examples }
            if !model.synonyms.isEmpty || !model.antonyms.isEmpty {
                let parts = [model.synonyms.isEmpty ? "" : "同 " + model.synonyms.prefix(6).joined(separator: ", "),
                             model.antonyms.isEmpty ? "" : "反 " + model.antonyms.prefix(6).joined(separator: ", ")]
                Text(parts.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 12)).foregroundStyle(WordPopStyle.muted).padding(.top, 8)
            }
            // 日语不出这个按钮：日语的「展开」内容（例句 / 汉字 / AI 深度解释）已在本框里。
            if !model.expanded, !model.isJapanese {
                Button {
                    Task { await model.expand() }
                } label: {
                    Text(model.expanding ? "展开中…" : "点这里展开完整字典 ▾")
                        .font(.system(size: 11)).foregroundStyle(WordPopStyle.accent)
                }
                .buttonStyle(.plain)
                .disabled(model.expanding)
                .padding(.top, 7)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14).padding(.top, 9).padding(.bottom, 11)
        .overlay(alignment: .top) { divider }
    }

    /// 母语例句（原版 .wp-ex：虚线分隔；日文一行、中文一行，中文缺了就地补）。
    private var examples: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(model.examples.enumerated()), id: \.offset) { index, pair in
                VStack(alignment: .leading, spacing: 2) {
                    Text(pair.0).font(.system(size: 13)).foregroundStyle(WordPopStyle.exJa).lineSpacing(3)
                    let zh = pair.1.isEmpty ? (model.exampleZh[index] ?? "") : pair.1
                    if !zh.isEmpty {
                        Text(zh).font(.system(size: 12)).foregroundStyle(WordPopStyle.exZh)
                    } else if model.exampleZhPending.contains(index) {
                        Text("翻译中…").font(.system(size: 11)).foregroundStyle(WordPopStyle.dim)
                    }
                }
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 9)
        .overlay(alignment: .top) {
            DashLine().stroke(style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                .foregroundStyle(WordPopStyle.rule)
                .frame(height: 1)
        }
        .padding(.top, 8)
    }

    /// 汉字（原版 .jp-kanji-row + .jp-kanji-detail：点字看音读 / 训读 / 字义，默认第一个）。
    private var kanjiSection: some View {
        let list = model.kanji
        let current = list.indices.contains(model.selectedKanji) ? list[model.selectedKanji] : list[0]
        return VStack(alignment: .leading, spacing: 8) {
            Text("汉字（点字看音读/训读）").font(.system(size: 11)).foregroundStyle(WordPopStyle.dim)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(list) { item in
                        kanjiChip(item, active: item.id == current.id)
                    }
                }
            }
            if !current.on.isEmpty || !current.kun.isEmpty || !current.meaning.isEmpty {
                HStack(alignment: .top, spacing: 12) {
                    Text(current.literal).font(.system(size: 36)).foregroundStyle(.white)
                    VStack(alignment: .leading, spacing: 4) {
                        if !current.on.isEmpty { readingRow("音", current.on, WordPopStyle.cyan) }
                        if !current.kun.isEmpty { readingRow("訓", current.kun, WordPopStyle.success) }
                        Text(current.meaning.isEmpty ? "暂无本地中文字义" : current.meaning)
                            .font(.system(size: 12)).foregroundStyle(WordPopStyle.exZh)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(WordPopStyle.surface, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(WordPopStyle.rule, lineWidth: 1))
            }
        }
        .padding(.horizontal, 14).padding(.top, 9).padding(.bottom, 11)
        .overlay(alignment: .top) { divider }
    }

    private func kanjiChip(_ item: ReaderNativeLookupModel.Kanji, active: Bool) -> some View {
        Button { model.selectedKanji = item.id } label: {
            Text(item.literal).font(.system(size: 22))
                .foregroundStyle(.white)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(active ? WordPopStyle.accent.opacity(0.26) : WordPopStyle.raised,
                            in: RoundedRectangle(cornerRadius: 9))
                .overlay(RoundedRectangle(cornerRadius: 9)
                    .stroke(active ? WordPopStyle.cyan : Color(red: 0x2e / 255, green: 0x3f / 255, blue: 0x63 / 255),
                            lineWidth: active ? 1.5 : 1))
        }
        .buttonStyle(.plain)
    }

    private func readingRow(_ tag: String, _ values: [String], _ color: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(tag).font(.system(size: 11))
                .foregroundStyle(Color(red: 0x0b / 255, green: 0x10 / 255, blue: 0x20 / 255))
                .frame(width: 18).background(color, in: RoundedRectangle(cornerRadius: 3))
            Text(values.joined(separator: "、")).font(.system(size: 13))
        }
    }

    /// AI 深度解释（原版 .jp-ai-btn：整宽按钮，结果接在下面）。
    private var aiSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.aiText.isEmpty {
                Button {
                    Task { await model.deepExplain() }
                } label: {
                    HStack(spacing: 6) {
                        if model.aiLoading { ProgressView().controlSize(.mini).tint(.white) }
                        Text(model.aiLoading ? "AI 深度解释中…" : "AI 深度解释（句境 / 用法 / 语感 / 近义辨析）")
                            .font(.system(size: 13))
                    }
                    .frame(maxWidth: .infinity).padding(.vertical, 9)
                    .background(Color(red: 0x1a / 255, green: 0x27 / 255, blue: 0x48 / 255),
                                in: RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(WordPopStyle.accentBorder, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .disabled(model.aiLoading)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(model.aiText.enumerated()), id: \.offset) { _, line in
                        Text(line).font(.system(size: 13)).lineSpacing(4).textSelection(.enabled)
                    }
                }
            }
            if let error = model.aiError {
                Text(error).font(.system(size: 12)).foregroundStyle(.orange)
            }
        }
        .padding(.horizontal, 14).padding(.top, 10).padding(.bottom, 12)
        .overlay(alignment: .top) { divider }
    }

    // MARK: 底部按钮条（原版 .wp-actions：黑底、等分按钮）

    private var masterButton: some View {
        barButton(model.mastered ? "已掌握 100" : "标记掌握",
                  icon: model.mastered ? "checkmark" : "star", on: model.mastered, busy: model.marking) {
            Task { await model.markMastered() }
        }
        .accessibilityHint("标记掌握后这个词不再画生词下划线；再点取消")
    }

    private func actionBar<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        HStack(spacing: 8) { content() }
            .padding(.horizontal, 14).padding(.vertical, 9)
            .frame(maxWidth: .infinity)
            .background(Color.black)
            .overlay(alignment: .top) { divider }
    }

    private func barButton(_ title: String, icon: String, on: Bool = false, busy: Bool = false,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.system(size: 12))
                .lineLimit(1).minimumScaleFactor(0.8)
                .foregroundStyle(on ? Color(red: 0x7e / 255, green: 0xe2 / 255, blue: 0xb8 / 255) : Color.white)
                .frame(maxWidth: .infinity).padding(.vertical, 8)
                .background(on ? Color(red: 0x13 / 255, green: 0x35 / 255, blue: 0x1f / 255) : WordPopStyle.raised,
                            in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6)
                    .stroke(on ? WordPopStyle.success : WordPopStyle.border, lineWidth: 1))
                .opacity(busy ? 0.6 : 1)
        }
        .buttonStyle(.plain)
        .disabled(busy)
    }

    private var divider: some View { Rectangle().fill(WordPopStyle.border).frame(height: 0.5) }
}

private struct DashLine: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        return path
    }
}

/// 日语音调线（原版 `_renderPitch`）：读音拆拍，高拍上方一条青线，下降处一道红竖线，末尾标型。
/// accent：0 = 平板（第 1 拍低、其余高），1 = 頭高（第 1 拍高、其余低），N = 第 N 拍后下降。
struct ReaderNativePitchView: View {
    let reading: String
    let accent: Int

    static func morae(_ reading: String) -> [String] {
        let small = "ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ"
        var out: [String] = []
        for ch in reading {
            if small.contains(ch), !out.isEmpty { out[out.count - 1].append(ch) } else { out.append(String(ch)) }
        }
        return out
    }

    private func high(_ i: Int) -> Bool {
        if accent == 0 { return i >= 1 }
        if accent == 1 { return i == 0 }
        return i >= 1 && i < accent
    }

    private var typeLabel: String { accent == 0 ? "平板" : accent == 1 ? "頭高" : "[\(accent)]" }

    var body: some View {
        let morae = Self.morae(reading)
        HStack(alignment: .center, spacing: 0) {
            ForEach(Array(morae.enumerated()), id: \.offset) { index, mora in
                Text(mora).font(.system(size: 15))
                    .foregroundStyle(high(index) ? Color.white : WordPopStyle.muted)
                    .padding(.horizontal, 1).padding(.top, 4)
                    .overlay(alignment: .top) {
                        Rectangle().fill(high(index) ? WordPopStyle.cyan : Color.clear).frame(height: 2)
                    }
                    .overlay(alignment: .topTrailing) {
                        if accent >= 1 && index + 1 == accent {
                            Rectangle().fill(WordPopStyle.drop).frame(width: 2, height: 9).offset(x: 1)
                        }
                    }
            }
            Text(typeLabel)
                .font(.system(size: 10)).foregroundStyle(WordPopStyle.dim)
                .padding(.horizontal, 4)
                .overlay(RoundedRectangle(cornerRadius: 4)
                    .stroke(Color(red: 0x2a / 255, green: 0x34 / 255, blue: 0x50 / 255), lineWidth: 1))
                .padding(.leading, 6)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("读音 \(reading)，声调 " + (accent == 0 ? "平板" : accent == 1 ? "頭高" : "第\(accent)拍后降"))
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
