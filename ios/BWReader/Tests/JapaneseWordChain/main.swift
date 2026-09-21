import Foundation

// 日语词链合并（ReaderJapaneseWordChain）的判据测试。
//
// 用户 2026-09-21 在页面上能单独点中「れ」，问「这里不应该是禁止的形态变化之一么
// 为何单独被分词了」。App 用的是 NLTokenizer，给的是裸形态素；服务端那套
// fugashi 分词一直会把变形并回词里。这里守的是那条差距被补上，**并且没有顺手
// 把助词也并进去** —— 把「を」「は」吃进词里会让点词查不到东西，比拆开更糟。

var failures = 0

func check(_ condition: Bool, _ reason: String) {
    if !condition {
        FileHandle.standardError.write(Data(("FAIL: " + reason + "\n").utf8))
        failures += 1
    }
}

/// 把 token 列表按「全部紧贴」算一遍组号（页面上同一行的相邻 token 就是这样）。
func groups(_ surfaces: [String]) -> [Int] {
    let adjacent = surfaces.enumerated().map { index, _ in index > 0 }
    return ReaderJapaneseWordChain.groupIDs(
        surfaces: surfaces, adjacentToPrevious: adjacent)
}

func allSame(_ surfaces: [String]) -> Bool {
    Set(groups(surfaces)).count == 1
}

// ── 用户报的那一句 ────────────────────────────────────────────────
// NLTokenizer 对「禁止されている」可能切成 禁止/さ/れ/て/いる，也可能是
// 禁止/され/て/いる。两种切法都必须收敛成一个词。
check(allSame(["禁止", "さ", "れ", "て", "いる"]), "禁止されている 应当是一个词（逐形态素）")
check(allSame(["禁止", "され", "て", "いる"]), "禁止されている 应当是一个词（され 合切）")
check(allSame(["就業", "し", "て", "いる"]), "就業している 应当是一个词")
check(allSame(["感染", "する"]), "サ变 名詞+する 应当合并")

// ── 助词绝不能被并走 ──────────────────────────────────────────────
// 并错了点词就查不到东西，比拆开更糟，所以这几条比上面更要紧。
check(groups(["本", "を", "読む"]) == [0, 1, 2], "格助词 を 不能并进前一个词")
check(groups(["患者", "と"]) == [0, 1], "并列助词 と 不能并走")
check(groups(["無症状", "の", "人"]) == [0, 1, 2], "の 不能并走")
check(groups(["電車", "で"]) == [0, 1], "で 既是て形浊化也是格助词，一律不并")
check(groups(["本", "です"]) == [0, 1], "断定助动词 です 不并进名词")
check(groups(["患者", "は"]) == [0, 1], "提示助词 は 不能并走")

// ── 不相邻就不合并 ────────────────────────────────────────────────
// 跨行/跨列的两段文字凑不成一个词。
check(
    ReaderJapaneseWordChain.groupIDs(
        surfaces: ["禁止", "され"], adjacentToPrevious: [false, false]) == [0, 1],
    "中间隔着空白/换行时不能合并")

// ── 没有汉字起头就不起链 ──────────────────────────────────────────
// 「れ」前面没有可依附的词时，它自己成组，不能凭空并到更前面去。
check(groups(["を", "れ"]) == [0, 1], "助词后面的 れ 不应被并走")
check(groups(["れ"]) == [0], "单个 token 就是它自己")

// ── 非日语不受影响 ────────────────────────────────────────────────
check(groups(["Primary", "Health", "Care"]) == [0, 1, 2], "英文分词保持原样")
check(groups([]) == [], "空输入安全")

// ── 链被打断后不能"复活" ──────────────────────────────────────────
// 「禁止 は て」：は 打断了链，后面的 て 不能再并回 禁止。
check(groups(["禁止", "は", "て"]) == [0, 1, 2], "助词打断词链后不能再续上")

if failures == 0 {
    print("OK: ReaderJapaneseWordChain checks passed")
} else {
    FileHandle.standardError.write(Data("\(failures) check(s) failed\n".utf8))
    exit(1)
}
