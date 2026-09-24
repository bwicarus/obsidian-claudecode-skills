import Foundation

/// Dictionary data and presentation rules, without a web popup or DOM. The
/// offline dictionary and existing server/ReaderPC fallback remain the sources.
enum ReaderNativeWordLookup {
    typealias Object = [String: Any]
    static func string(_ value: Any?) -> String { value as? String ?? "" }
    static func clean(_ text: String) -> String {
        text.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
    static func meaning(_ value: Object) -> String {
        var items: [String] = []
        func append(_ input: Any?) {
            var text = clean(string(input))
            guard !text.isEmpty,
                  text.range(of: #"^(未能确定|无法确定|不确定|不能确定|暂无法确定|未知|不明|无法判断|无法识别)"#, options: .regularExpression) == nil,
                  text.range(of: #"(?i)\b(alt-of|alternative\s+(form|spelling|kanji)|redirected\s+from|romanization|non-lemma|stem|continuative|imperfective|attributive)\b"#, options: .regularExpression) == nil else { return }
            text = text.replacingOccurrences(of: #"(?i)^onoma\s*"#, with: "", options: .regularExpression)
            guard text.unicodeScalars.contains(where: { (0x3400...0x9fff).contains($0.value) }), !items.contains(text) else { return }
            items.append(text)
        }
        for sense in value["zh_senses"] as? [Object] ?? [] where !string(sense["pos"]).lowercased().contains("non-lemma") {
            for gloss in sense["glosses"] as? [Any] ?? [] { append(gloss) }
        }
        if items.isEmpty { append(value["zh"]) }
        if items.isEmpty, string(value["meaning_language"]) != "en" {
            append(string(value["translation"]).isEmpty ? value["definition"] : value["translation"])
        }
        return items.joined(separator: "；")
    }

    static func merge(local: Object, remote: Object) -> Object {
        guard remote["ok"] as? Bool == true else { return local.isEmpty ? remote : local }
        guard local["ok"] as? Bool == true else {
            return ["source": "pi-dict-quick", "meaning_source": "pi-dict-quick"].merging(remote) { _, new in new }
        }
        var result = local
        if remote["stale"] as? Bool == true { result["stale"] = true }
        for key in ["source_word", "source_lang", "source_kind"] where clean(string(result[key])).isEmpty {
            let text = clean(string(remote[key])); if !text.isEmpty { result[key] = text }
        }
        if meaning(result).isEmpty, !meaning(remote).isEmpty {
            result["zh"] = meaning(remote); result["translation"] = meaning(remote); result["meaning_source"] = "pi-dict-quick"
        }
        let examples = local["examples"] as? [Object] ?? [], remoteExamples = remote["examples"] as? [Object] ?? []
        if examples.isEmpty { result["examples"] = remoteExamples }
        else {
            var chinese: [String: String] = [:]
            for item in remoteExamples {
                let ja = clean(string(item["ja"])), zh = clean(string(item["zh"]))
                if !ja.isEmpty, !zh.isEmpty { chinese[ja] = zh }
            }
            result["examples"] = examples.map { item -> Object in
                var item = item
                if string(item["zh"]).isEmpty, let zh = chinese[clean(string(item["ja"]))] { item["zh"] = zh }
                return item
            }
        }
        return result
    }

    @MainActor
    static func lookup(japanese: Bool, local: () async throws -> Object,
                       remote: () async throws -> Object, fallback: (Object) async throws -> Object?) async throws -> Object {
        try Task.checkCancellation()
        if !japanese { return try await remote() }
        let original: Object
        do { original = try await local() }
        catch { if error is CancellationError || Task.isCancelled { throw CancellationError() }; original = [:] }
        try Task.checkCancellation()
        if original["ok"] as? Bool == true, !meaning(original).isEmpty { return original }
        var result = original
        do { result = merge(local: original, remote: try await remote()) }
        catch { if error is CancellationError || Task.isCancelled { throw CancellationError() } }
        try Task.checkCancellation()
        if !meaning(result).isEmpty { return result }
        do { if let value = try await fallback(result) { result = value } }
        catch { if error is CancellationError || Task.isCancelled { throw CancellationError() } }
        try Task.checkCancellation()
        if meaning(result).isEmpty { result["ok"] = false }
        return result
    }

    static func cacheable(_ value: Object, japanese: Bool) -> Bool {
        value["ok"] as? Bool == true && value["stale"] as? Bool != true && (!japanese || !meaning(value).isEmpty)
            && string(value["meaning_source"]) != "synthetic" && !string(value["definition"]).hasPrefix("暂无词典释义")
    }

    static func entry(_ value: Object, word: String, japanese: Bool, mastered: Bool) throws -> Object {
        let jp = japanese || value["jp"] as? Bool == true, missing = value["ok"] as? Bool != true
        guard jp || !missing else { throw ReaderNativeLookupRequest.Failure(message: "BW_READER_LOOKUP_MISS") }
        let description = jp ? meaning(value) : string(value["translation"])
        var source = ""
        if string(value["meaning_source"]) == "pc-codex-cli" {
            source = "电脑 ReaderPC · Codex CLI 上下文中文释义" + (value["cli_cached"] as? Bool == true ? " · 本地缓存" : "")
        } else if string(value["source"]) == "local-jmdict" {
            source = "App 本地 JMdict" + (value["local_zh"] as? Bool == true ? " · 中文 Wiktionary 释义" : " · 暂无本地中文释义")
        }
        var examples: [Object] = [], seen = Set<String>()
        let all = (value["zh_senses"] as? [Object] ?? []).flatMap { $0["examples"] as? [Object] ?? [] }
            + (value["examples"] as? [Object] ?? [])
        for item in all where jp {
            let ja = clean(string(item["ja"]))
            if !ja.isEmpty, seen.insert(ja).inserted { examples.append(["ja": ja, "zh": clean(string(item["zh"]))]) }
            if examples.count == 8 { break }
        }
        let kanji = (value["kanji"] as? [Object] ?? []).prefix(12).compactMap { item -> Object? in
            guard !string(item["kanji"]).isEmpty else { return nil }
            return ["kanji": string(item["kanji"]), "on": strings(item["on"], maximum: 8),
                    "kun": strings(item["kun"], maximum: 8), "meaning": string(item["meanings_zh"])]
        }
        let accent: Any = (jp ? (value["accent"] as? NSNumber).flatMap { $0.doubleValue.isFinite ? $0 : nil } : nil) as Any? ?? NSNull()
        return ["mode": "dict", "jp": jp, "missing": missing, "word": word, "lemma": string(value["lemma"]),
                "reading": string(value["reading"]), "accent": accent, "phonetic": string(value["phonetic"]),
                "freq": value["freq_bnc"] as? NSNumber ?? 0, "pos": jp ? "" : string(value["pos"]),
                "meaning": description, "zh": description, "translation": description,
                "definition": jp ? "" : String(string(value["definition"]).prefix(4000)),
                "inflect": String(inflection(value, word: word, japanese: jp).prefix(400)),
                "origin": String(origin(value).prefix(400)), "source": source, "examples": examples,
                "kanji": kanji, "mastered": mastered, "stale": value["stale"] as? Bool == true]
    }

    static func strings(_ input: Any?, maximum: Int) -> [String] {
        Array((input as? [String] ?? []).map(clean).filter { !$0.isEmpty }.prefix(maximum))
    }
    static func inflection(_ value: Object, word: String, japanese: Bool) -> String {
        let lemma = string(value["lemma"])
        if !japanese {
            let lemma = (lemma.isEmpty ? word : lemma).lowercased()
            var seen = Set<String>()
            let forms = strings(value["forms"], maximum: 100).map { $0.lowercased() }
                .filter { $0 != lemma && seen.insert($0).inserted }.prefix(8)
            return [word.lowercased() != lemma ? "原型 " + lemma : "", forms.isEmpty ? "" : "变形 " + forms.joined(separator: "・")]
                .filter { !$0.isEmpty }.joined(separator: " ")
        }
        let inf = value["inflect"] as? Object ?? [:]
        let surface = clean(string(inf["surface"]).isEmpty ? word : string(inf["surface"]))
        let base = clean(string(inf["base"]).isEmpty ? lemma : string(inf["base"]))
        var different = !surface.isEmpty && !base.isEmpty && surface != base
        var marks = strings(inf["marks"], maximum: 100)
        if different, !string(inf["variant"]).isEmpty {
            return [(string(inf["variant"]) == "reading" ? "读音 " : "写法 ") + surface,
                    "词头 " + base, marks.joined(separator: "・")].filter { !$0.isEmpty }.joined(separator: " ")
        }
        if different, marks.isEmpty {
            func normalized(_ text: String) -> String { text.replacingOccurrences(of: #"[・･·\s　]+"#, with: "", options: .regularExpression) }
            different = normalized(surface) != normalized(base)
            if different { marks = ["活用→原形"] }
        }
        return [different ? "当前形 " + surface : "", different ? "原形 " + base : "", marks.joined(separator: "・")]
            .filter { !$0.isEmpty }.joined(separator: " ")
    }
    static func origin(_ value: Object) -> String {
        guard value["jp"] as? Bool == true else { return "" }
        let word = clean(string(value["source_word"])), kind = clean(string(value["source_kind"]))
        guard !word.isEmpty || kind == "wasei" else { return "" }
        let lang = clean(string(value["source_lang"])).lowercased()
        let labels = ["en":"英語", "de":"德語", "pt":"葡萄牙語", "fr":"法語", "nl":"荷蘭語", "it":"意大利語",
                      "ru":"俄語", "es":"西班牙語", "la":"拉丁語", "zh":"中文", "ko":"韓語", "th":"泰語", "ar":"阿拉伯語"]
        return ["源词 " + (word.isEmpty ? "—" : word), kind == "wasei" ? "和製英語" : labels[lang] ?? lang.uppercased()]
            .filter { !$0.isEmpty }.joined(separator: " ")
    }
}
