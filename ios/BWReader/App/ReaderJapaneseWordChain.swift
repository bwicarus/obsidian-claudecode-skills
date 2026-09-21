import Foundation

/// 把日语的变形尾巴并回它所属的词，让「禁止されている」是**一个可点的词**。
///
/// 用户 2026-09-21 实录：页面上能单独点中「れ」，问「这里不应该是禁止的形态变化
/// 之一么为何单独被分词了」。
///
/// 原因是两侧用的不是同一个分词器：
///
/// | 表面 | 分词器 | 有没有合并变形 |
/// |---|---|---|
/// | 桌面 / 扩展 | 服务端 fugashi + unidic（`pdf_reader.py::_apply_jp_tokenize`） | 有 —— サ变、助動詞、接続助詞て、補助動詞 都并进动词链 |
/// | App（`/pdf/api/page-chars` 本地执行） | `NLTokenizer(unit: .word)` | **没有**，给的是裸形态素 |
///
/// 所以同一本书在桌面上「禁止されている」是一个词，在 App 上散成五个。
///
/// ## 为什么不照搬服务端那套判据
///
/// 服务端的规则读的是 unidic 词性（助動詞 / 接続助詞 / 非自立動詞）。`NLTagger`
/// 的日语 `lexicalClass` 没有这些类别，照搬会变成「按一个拿不到的字段分支」。
/// 这里改成**按表层闭集**判断：只有列在 `attachable` 里的尾巴才会被并走，
/// 没列的一律保持今天的行为。宁可少并，不要把助词（は/が/を/に/の）并进前一个词
/// —— 那会让点词查不到东西，比拆开更糟。
///
/// ⚠ 刻意**不收** 「で」「です」「だ」：
///   · 「で」既是て形浊化也是格助词（「電車で」），并错了就把助词吃进词里；
///   · 「です」「だ」是断定助动词，接在名词后面并进去等于把「本です」当一个词。
enum ReaderJapaneseWordChain {
    /// サ变：接在含汉字的词后面时，把「する」的活用并进那个名词（禁止 + さ）。
    private static let suruStems: Set<String> = [
        "さ", "し", "す", "せ", "する", "され", "されて", "して", "した", "しない",
        "します", "しました", "しよう", "できる", "でき"
    ]

    /// 已经在一个词链里时可以继续并进来的尾巴：助动词、て、以及て后面的补助动词。
    private static let attachable: Set<String> = [
        // 受身 / 使役 / 可能
        "れ", "れる", "られ", "られる", "せ", "させ", "させる", "せる",
        // 过去 / 否定 / 丁宁
        "た", "たら", "たり", "ない", "なく", "なかっ", "なかった", "ず", "ぬ",
        "ます", "まし", "ました", "ません", "ましょう", "ん",
        // 接続助詞て（浊音「で」刻意不收，见类型说明）
        "て",
        // て的后面常接的补助动词
        "いる", "いた", "います", "いました", "いない", "いなかった",
        "ある", "あっ", "あり", "おり", "おら", "しまう", "しまっ", "しまった",
        "みる", "みた", "おく", "おい", "くる", "きた", "ください", "いき", "いく",
        // 其它常见变形尾巴
        "たい", "たく", "たかっ", "そう", "よう", "ば", "れば", "ながら"
    ]

    private static func isAllHiragana(_ text: String) -> Bool {
        guard !text.isEmpty else { return false }
        return text.unicodeScalars.allSatisfy { (0x3041...0x309F).contains($0.value) }
    }

    private static func containsKanji(_ text: String) -> Bool {
        text.unicodeScalars.contains { scalar in
            (0x4E00...0x9FFF).contains(scalar.value)
                || (0x3400...0x4DBF).contains(scalar.value)
        }
    }

    /// 词链分组。
    ///
    /// - Parameters:
    ///   - surfaces: 逐个 token 的表层文字，顺序与页面一致。
    ///   - adjacentToPrevious: 这个 token 是否**紧贴**上一个（中间没有空白/换行）。
    ///     不紧贴就绝不合并 —— 跨行跨列的两段文字凑不成一个词。
    /// - Returns: 与 `surfaces` 等长的组号；组号相同 = 点一下选中同一个词。
    static func groupIDs(surfaces: [String], adjacentToPrevious: [Bool]) -> [Int] {
        var groups: [Int] = []
        groups.reserveCapacity(surfaces.count)
        var chainGroup: Int?          // 当前词链的组号
        var previousSurface = ""
        for (index, surface) in surfaces.enumerated() {
            let adjacent = index < adjacentToPrevious.count
                ? adjacentToPrevious[index] : false
            let canContinue = adjacent && index > 0
            var group = index
            if canContinue, isAllHiragana(surface) {
                if suruStems.contains(surface), containsKanji(previousSurface) {
                    // サ变：名詞 + する 的活用 → 并进那个名词（禁止 + さ）
                    group = groups[index - 1]
                    chainGroup = group
                } else if let chain = chainGroup, attachable.contains(surface) {
                    group = chain
                } else {
                    chainGroup = nil
                }
            } else {
                // 含汉字的词自己起一条链：它后面的尾巴才有东西可并
                chainGroup = containsKanji(surface) ? index : nil
            }
            groups.append(group)
            previousSurface = surface
        }
        return groups
    }
}
