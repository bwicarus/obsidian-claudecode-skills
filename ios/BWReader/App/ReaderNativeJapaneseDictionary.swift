import Foundation
import CoreFoundation

/// The installed v3 dictionary is unchanged. Swift owns lookup, inflection and
/// rich result construction; callers supply the existing verified resource reader.
final class ReaderNativeJapaneseDictionary {
    typealias Object = [String: Any]
    struct InvalidResource: Error {}
    let read: (String) throws -> Data
    private let resources = NSCache<NSString, NSDictionary>()

    init(read: @escaping (String) throws -> Data) {
        self.read = read
        resources.countLimit = 12
        resources.totalCostLimit = 24 * 1024 * 1024
    }

    func clear() { resources.removeAllObjects() }

    private func resource(_ path: String) throws -> Object {
        guard path == "manifest.json" || path == "kanji.json" || Self.matches(path, #"^shards/[a-f0-9]{1,6}\.json$"#) else { throw InvalidResource() }
        if let cached = resources.object(forKey: path as NSString) { return cached as! Object }
        let bytes = try read(path)
        guard let value = try JSONSerialization.jsonObject(with: bytes) as? Object else { throw InvalidResource() }
        // Cost includes an allowance for decoded strings and Foundation objects.
        // Oversized shards are used for this lookup only, never kept indefinitely.
        if bytes.count <= 6 * 1024 * 1024 {
            resources.setObject(value as NSDictionary, forKey: path as NSString, cost: bytes.count * 4)
        }
        return value
    }

    static func normalize(_ text: String) -> String {
        text.trimmingCharacters(in: .whitespacesAndNewlines).precomposedStringWithCanonicalMapping
    }
    static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }
    private static func replace(_ text: String, _ pattern: String, _ replacement: String) -> String {
        text.replacingOccurrences(of: pattern, with: replacement, options: .regularExpression)
    }
    static func shardKey(_ term: String) -> String {
        let bytes = Array(normalize(term).utf8)
        let kana = bytes.count >= 2 && bytes[0] == 0xe3 && [0x81, 0x82, 0x83].contains(bytes[1])
        return bytes.prefix(kana ? 3 : 2).map { String(format: "%02x", $0) }.joined()
    }
    static func matchKind(_ entry: Object, _ term: String) -> String {
        if normalize(entry["lemma"] as? String ?? "") == term { return "lemma" }
        let readings = (entry["readings"] as? [String] ?? []).map(normalize)
        if readings.first == term { return "reading" }
        if (entry["forms"] as? [String] ?? []).map(normalize).contains(term) { return "form" }
        return readings.contains(term) ? "rare-reading" : "other"
    }
    static func hira(_ text: String) -> String {
        String(String.UnicodeScalarView(text.unicodeScalars.map { scalar in
            (0x30a1...0x30f6).contains(scalar.value) ? UnicodeScalar(scalar.value - 0x60)! : scalar
        }))
    }
    static func moraCount(_ text: String) -> Int {
        hira(text).unicodeScalars.filter { !"ゃゅょぁぃぅぇぉゎ".unicodeScalars.contains($0) }.count
    }
    static let romanization: [String: String] = {
        let rows = [
            ("きゃ きゅ きょ しゃ しゅ しょ ちゃ ちゅ ちょ", "kya kyu kyo sha shu sho cha chu cho"),
            ("にゃ にゅ にょ ひゃ ひゅ ひょ みゃ みゅ みょ", "nya nyu nyo hya hyu hyo mya myu myo"),
            ("りゃ りゅ りょ ぎゃ ぎゅ ぎょ じゃ じゅ じょ", "rya ryu ryo gya gyu gyo ja ju jo"),
            ("びゃ びゅ びょ ぴゃ ぴゅ ぴょ", "bya byu byo pya pyu pyo"),
            ("あ い う え お か き く け こ", "a i u e o ka ki ku ke ko"),
            ("さ し す せ そ た ち つ て と", "sa shi su se so ta chi tsu te to"),
            ("な に ぬ ね の は ひ ふ へ ほ", "na ni nu ne no ha hi fu he ho"),
            ("ま み む め も や ゆ よ", "ma mi mu me mo ya yu yo"),
            ("ら り る れ ろ わ を ん", "ra ri ru re ro wa o n"),
            ("が ぎ ぐ げ ご ざ じ ず ぜ ぞ", "ga gi gu ge go za ji zu ze zo"),
            ("だ ぢ づ で ど ば び ぶ べ ぼ", "da ji zu de do ba bi bu be bo"),
            ("ぱ ぴ ぷ ぺ ぽ ゔ", "pa pi pu pe po vu")
        ]
        return Dictionary(uniqueKeysWithValues: rows.flatMap { a, b in zip(a.split(separator: " "), b.split(separator: " ")).map { (String($0.0), String($0.1)) } })
    }()
    static func romaji(_ text: String) -> String {
        let units = Array(hira(text).unicodeScalars).map(String.init)
        var result = "", doubled = false, index = 0
        while index < units.count {
            let unit = units[index]; index += 1
            if unit == "っ" { doubled = true; continue }
            if unit == "ー" {
                if let last = result.last, "aeiou".contains(last) { result.append(last) }
                continue
            }
            let pair = unit + (index < units.count ? units[index] : "")
            let piece: String
            if let combined = romanization[pair] { piece = combined; index += 1 }
            else { piece = romanization[unit] ?? unit }
            if doubled, let first = piece.first, "bcdfghjklmnpqrstvwxyz".contains(first) { result.append(first) }
            doubled = false; result += piece
        }
        return result
    }

    static let auxiliaries: [(String, String)] = [
        (#"(て|で)(?:いる|います|いた|いました|いない|いません|いなかった|いて|る|た|ます|ない|なかった)$"#, "进行・状态(ている)"),
        (#"(て|で)(?:しまう|しまった|しまいます|しまいました|ちゃう|ちゃった|じゃう|じゃった)$"#, "完了・遗憾(てしまう)"),
        (#"(て|で)(?:おく|おいた|おきます|おきました|とく|といた)$"#, "预先(ておく)"),
        (#"(て|で)(?:ある|あった|あります|ありました)$"#, "结果状态(てある)"),
        (#"(て|で)(?:みる|みた|みます|みました|みたい)$"#, "尝试(てみる)"),
        (#"(て|で)(?:いく|いった|いきます|いきました|くる|きた|きます|きました)$"#, "方向・变化(ていく／てくる)"),
        (#"(て|で)(?:ください|くれる|くれた|くれます|もらう|もらった|もらいます|あげる|あげた|あげます|やる|やった)$"#, "授受(てくれる／てもらう／てあげる)"),
        (#"(て|で)(?:ほしい|ほしかった)$"#, "希望(てほしい)")
    ]
    private static func baseFromTe(_ stem: String) -> [String] {
        var result: [String] = []
        func add(_ pattern: String, _ replacements: [String]) {
            if matches(stem, pattern) { result += replacements.map { replace(stem, pattern, $0) } }
        }
        add("して$", ["す", "する"])
        if matches(stem, "来て$|きて$") { result.append(replace(replace(stem, "(来|き)て$", "$1る"), "きる$", "くる")) }
        add("行って$", ["行く"]); add("いて$", ["く"]); add("いで$", ["ぐ"])
        add("って$", ["う", "つ", "る"]); add("んで$", ["む", "ぶ", "ぬ"])
        add("て$", ["る"]); add("で$", ["る"])
        return result
    }
    static func candidateForms(_ term: String) -> [[String: String]] {
        let value = normalize(term)
        var result = [["term": value, "mark": ""]], seen: Set<String> = [value]
        func add(_ input: String, _ mark: String) {
            let candidate = normalize(input)
            if !candidate.isEmpty, seen.insert(candidate).inserted { result.append(["term": candidate, "mark": mark]) }
        }
        func suffix(_ pattern: String, _ replacements: [String], _ mark: String) {
            if matches(value, pattern) { replacements.forEach { add(replace(value, pattern, $0), mark) } }
        }
        for (pattern, mark) in auxiliaries {
            if matches(value, pattern) {
                let stem = replace(value, pattern, "$1")
                if stem.utf16.count < 2 { continue }
                baseFromTe(stem).forEach { add($0, mark + "・て形→原形") }; break
            }
        }
        let ren = ["い":"う", "き":"く", "ぎ":"ぐ", "し":"す", "ち":"つ", "に":"ぬ", "び":"ぶ", "み":"む", "り":"る"]
        let desire = "(たい|たかった|たくない|たくて)$"
        if matches(value, desire) {
            let stem = replace(value, desire, "")
            if !stem.isEmpty {
                if let last = stem.last, let ending = ren[String(last)] { add(String(stem.dropLast()) + ending, "愿望(たい)→原形") }
                add(stem + "る", "愿望(たい)→原形")
            }
        }
        if value.hasSuffix("せ") { add(value + "る", "连用形→原形") }
        suffix("して(?:いる|いた|いて)?$", ["する"], "サ变→原形")
        suffix("した$", ["する"], "サ变过去式→原形"); suffix("した$", ["す"], "过去式→原形")
        suffix("かった$", ["い"], "形容词过去式→原形"); suffix("く(?:ない|て)$", ["い"], "形容词活用→原形")
        suffix("った$", ["う", "つ", "る"], "过去式→原形"); suffix("んだ$", ["む", "ぶ", "ぬ"], "过去式→原形")
        suffix("いた$", ["く"], "过去式→原形"); suffix("いだ$", ["ぐ"], "过去式→原形")
        suffix("[てた]$", ["る"], "活用→原形")
        for (ending, table, mark) in [("ない", ["わ":"う", "か":"く", "が":"ぐ", "さ":"す", "た":"つ", "な":"ぬ", "ば":"ぶ", "ま":"む", "ら":"る"], "否定形→原形"), ("ます", ren, "ます形→原形")] {
            if value.hasSuffix(ending) {
                let stem = String(value.dropLast(ending.count))
                if let last = stem.last, let base = table[String(last)] { add(String(stem.dropLast()) + base, mark) }
                add(stem + "る", mark)
            }
        }
        return Array(result.prefix(18))
    }

    static func cleanGloss(_ value: String, lemma: String) -> String {
        var text = replace(normalize(value), #"\s+"#, " ")
        let head = normalize(lemma)
        if !head.isEmpty, text.hasPrefix(head + "【"), let end = text.firstIndex(of: "】") {
            text = replace(String(text[text.index(after: end)...]), #"^[\s：:、，。]+"#, "")
        }
        if matches(text, #"(?i)(?:\balt-of\b|\balternative\s+(?:form|spelling|kanji)\b|\bredirected\s+from\b|\bromanization\b|\bnon-lemma\b|\b(?:stem|continuative|imperfective|attributive)\b)"#) { return "" }
        text = normalize(replace(text, #"(?i)^onoma\s*"#, ""))
        return matches(text, "[㐀-鿿]") ? text : ""
    }
    static func chineseSenses(_ raw: Any?, lemma: String) -> [Object] {
        (raw as? [Object] ?? []).compactMap { sense in
            guard !matches(sense["pos"] as? String ?? "", "(?i)non-lemma") else { return nil }
            let glosses = unique((sense["glosses"] as? [String] ?? []).map { cleanGloss($0, lemma: lemma) }.filter { !$0.isEmpty })
            guard !glosses.isEmpty else { return nil }
            var result: Object = ["glosses": glosses]
            if let examples = sense["examples"] as? [Any], !examples.isEmpty { result["examples"] = Array(examples.prefix(5)) }
            return result
        }
    }
    private static func unique(_ values: [String]) -> [String] {
        var seen = Set<String>(); return values.filter { seen.insert($0).inserted }
    }

    func lookup(_ term: String, legacy: Bool = true) throws -> Object {
        let query = Self.normalize(term)
        if query.isEmpty { return ["ok": false, "source": "local-jmdict", "code": "BW_OFFLINE_DICTIONARY_EMPTY"] }
        guard query.utf16.count <= 2000 else { throw InvalidResource() }
        let manifest = try resource("manifest.json")
        guard manifest["contract"] as? String == "bw-jmdict-manifest/3", manifest["shardAlgorithm"] as? String == "utf8-prefix-2-kana-3/1",
              let shards = manifest["shards"] as? Object else { throw InvalidResource() }
        for form in Self.candidateForms(query) {
            let candidate = form["term"]!, key = Self.shardKey(candidate)
            guard let metadata = shards[key] as? Object else { continue }
            let shard = try resource(metadata["path"] as? String ?? "shards/\(key).json")
            guard shard["contract"] as? String == "bw-jmdict-shard/3", shard["key"] as? String == key,
                  let entries = shard["entries"] as? [Object], let exact = shard["exact"] as? Object else { throw InvalidResource() }
            let found = (exact[candidate] as? [Int] ?? []).compactMap { entries.indices.contains($0) ? entries[$0] : nil }
            guard !found.isEmpty else { continue }
            let rank = ["lemma":4, "reading":3, "form":2, "rare-reading":1, "other":0]
            let ordered = found.enumerated().sorted { a, b in
                let first = rank[Self.matchKind(a.element, candidate)] ?? -1, second = rank[Self.matchKind(b.element, candidate)] ?? -1
                if first != second { return first > second }
                let ac = a.element["common"] as? Bool == true, bc = b.element["common"] as? Bool == true
                return ac != bc ? ac : a.offset < b.offset
            }.map(\.element)
            let source = manifest["source"] as? Object ?? [:]
            let result: Object = ["ok":true, "source":"local-jmdict", "query":query, "matchedTerm":candidate,
                "matchKind":Self.matchKind(ordered[0], candidate), "inflectionMark":form["mark"]!, "entry":ordered[0],
                "candidates":ordered, "posLabels":manifest["posLabels"] ?? Object(),
                "sourceVersion":source["release"] ?? source["dictionaryVersion"] ?? source["version"] ?? ""]
            if legacy { return try legacyResult(result, original: term) }
            return result
        }
        return ["ok":false, "source":"local-jmdict", "query":query, "code":"BW_OFFLINE_DICTIONARY_NO_MATCH"]
    }

    private func legacyResult(_ result: Object, original: String) throws -> Object {
        let entry = result["entry"] as! Object
        let lemma = (entry["lemma"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? result["matchedTerm"] as? String ?? original
        let senses = Self.chineseSenses(entry["zhSenses"], lemma: lemma)
        var glosses = Self.unique((entry["zhGlosses"] as? [String] ?? []).map { Self.cleanGloss($0, lemma: lemma) }.filter { !$0.isEmpty })
        if glosses.isEmpty { glosses = Self.unique(senses.flatMap { $0["glosses"] as? [String] ?? [] }) }
        let chinese = glosses.prefix(4).joined(separator:"；"), hasChinese = !glosses.isEmpty
        let english = (entry["glosses"] as? [String] ?? []).filter { !$0.isEmpty }.prefix(6).joined(separator:"; ")
        let readings = (entry["readings"] as? [String] ?? []).filter { !$0.isEmpty }, reading = readings.first ?? ""
        let labels = result["posLabels"] as? [String:String] ?? [:]
        let pos = (entry["pos"] as? [String]).map { $0.map { labels[$0] ?? $0 }.joined(separator:" / ") } ?? entry["pos"] as? String ?? ""
        let surface = Self.normalize(original), mark = result["inflectionMark"] as? String ?? ""
        var marks = mark.isEmpty ? [] : [mark], variant = ""
        if !lemma.isEmpty, !surface.isEmpty, lemma != surface, marks.isEmpty {
            var kind = Self.matchKind(entry, surface)
            if kind == "form", Self.normalize(lemma).hasPrefix(surface), Self.normalize(lemma) != surface { kind = "stem" }
            if kind == "form" { variant = "form"; marks.append("同词异写") }
            else if kind == "reading" || kind == "rare-reading" { variant = "reading"; marks.append("按读音命中") }
            else { marks.append("活用→原形") }
        }
        let chars = Self.unique(lemma.unicodeScalars.map(String.init).filter { Self.matches($0, "[㐀-鿿]") })
        var kanji: Object = [:]
        if !chars.isEmpty { kanji = try resource("kanji.json") }
        let accent: Any
        if let n = entry["accent"] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite, n.doubleValue.rounded() == n.doubleValue { accent = n } else { accent = NSNull() }
        let inflect: Any = !lemma.isEmpty && !surface.isEmpty && (lemma != surface || !marks.isEmpty)
            ? ["base":lemma, "surface":surface, "marks":marks, "variant":variant] as Object : NSNull()
        return ["ok":true, "jp":true, "word":original, "lemma":lemma,
            "forms":(entry["forms"] as? [String] ?? []).filter { !$0.isEmpty }, "reading":reading,
            "reading_kata":entry["readingKata"] as? String ?? "", "accent":accent, "mora":Self.moraCount(reading), "romaji":Self.romaji(reading),
            "pos":pos, "zh":chinese, "translation":hasChinese ? chinese : english, "definition":hasChinese ? chinese : english,
            "examples":Array((entry["examples"] as? [Any] ?? []).prefix(5)), "zh_senses":senses,
            "etymology":entry["etymology"] as? [Any] ?? [], "synonyms":entry["synonyms"] as? [Any] ?? [], "source_urls":entry["sourceUrls"] as? [Any] ?? [],
            "kanji":chars.compactMap { kanji[$0] }, "inflect":inflect, "local_candidates":result["candidates"] ?? [],
            "source":"local-jmdict", "source_version":result["sourceVersion"] ?? "", "local_zh":hasChinese,
            "meaning_language":hasChinese ? "zh" : "en", "english_fallback":!hasChinese]
    }
}
