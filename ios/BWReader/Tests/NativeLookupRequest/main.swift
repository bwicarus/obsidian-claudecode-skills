import Foundation
let plan = try ReaderNativeLookupRequest(["mode": "translate", "text": " 日本語 "], file: "localbook:test", languages: ["ja"])
precondition(plan.path == "/pdf/api/translate-sentence" && plan.method == "POST")
let translationBody = try JSONSerialization.jsonObject(with: plan.body) as! [String: String]
precondition(translationBody == ["text": "日本語"])
let translation = try plan.decode(status: 200, data: Data(#"{"ok":true,"zh":"日语"}"#.utf8))
precondition(translation["zh"] as? String == "日语" && translation["text"] as? String == "日本語")
let example = try ReaderNativeLookupRequest(["mode": "example-zh", "text": "例文"], file: "localbook:test", languages: [])
let exampleBody = try JSONSerialization.jsonObject(with: example.body) as! [String: String]
precondition(exampleBody["backend"] == "ai")
let englishOnly = try example.decode(status: 200, data: Data(#"{"ok":true,"zh":"English only"}"#.utf8))
precondition(englishOnly["zh"] as? String == "", "English must not masquerade as a Chinese example")
for payload in [#"{"ok":false}"#, #"{"ok":1,"zh":"错误"}"#, #"[]"#] {
    do { _ = try plan.decode(status: 200, data: Data(payload.utf8)); preconditionFailure("invalid response accepted") }
    catch { }
}
do { _ = try plan.decode(status: 503, data: Data(#"{"ok":true,"zh":"错误"}"#.utf8)); preconditionFailure("HTTP error accepted") } catch {}
precondition(ReaderNativeLookupRequest.isJapanese("かな", languages: ["en"]))
precondition(ReaderNativeLookupRequest.isJapanese("漢字", languages: []))
precondition(!ReaderNativeLookupRequest.isJapanese("漢字", languages: ["zh"]))
do { _ = try ReaderNativeLookupRequest(["mode": "dict-full", "text": "日本"], file: "localbook:test", languages: ["ja"]); preconditionFailure("Japanese full dictionary routed to English") } catch {}
let full = try ReaderNativeLookupRequest(["mode": "dict-full", "text": "a&b", "page": 2, "context": "x+y#z"], file: "localbook:test", languages: ["en"])
let query = Dictionary(uniqueKeysWithValues: URLComponents(string: full.path)!.queryItems!.map { ($0.name, $0.value!) })
precondition(query["word"] == "a&b" && query["context"] == "x+y#z" && query["file"] == "localbook:test" && query["page"] == "2")
precondition(full.path.contains("x%2By"))
let response: [String: Any] = ["ok": true, "word": "word", "examples": Array(repeating: ["en": "sentence", "zh": "中文"], count: 9), "synonyms": ["one"], "definition": String(repeating: "x", count: 4010)]
let entry = try full.decode(status: 200, data: JSONSerialization.data(withJSONObject: response))
precondition((entry["examples"] as! [Any]).count == 6 && (entry["definition"] as! String).count == 4000)
precondition(entry["full"] as? Bool == true && entry["jp"] as? Bool == false)
print("Native lookup: request shape, dictionary language routing, result fields and failure gates passed")

var remoteCount = 0
let direct = try await plan.response(directTranslation: { "直接翻译" }, remote: {
    remoteCount += 1; return (500, Data())
})
precondition(direct["zh"] as? String == "直接翻译" && remoteCount == 0)
let fallback = try await plan.response(directTranslation: { throw ReaderNativeLookupRequest.Failure(message: "offline") }, remote: {
    remoteCount += 1; return (200, Data(#"{"ok":true,"zh":"后备翻译"}"#.utf8))
})
precondition(fallback["zh"] as? String == "后备翻译" && remoteCount == 1)
do {
    _ = try await plan.response(directTranslation: { throw CancellationError() }, remote: {
        remoteCount += 1; return (500, Data())
    })
    preconditionFailure("cancelled lookup continued")
} catch is CancellationError {}
precondition(remoteCount == 1)
let dictionary = try await full.response(directTranslation: { preconditionFailure("dictionary entered translator") }, remote: {
    (200, try JSONSerialization.data(withJSONObject: response))
})
precondition(dictionary["full"] as? Bool == true)
print("Native lookup routing: direct-first, remote fallback and cancellation passed")

let rich: [String: Any] = ["ok": true, "jp": true, "word": "幼な子", "lemma": "幼子", "reading": "おさなご", "accent": 3,
    "source": "local-jmdict", "local_zh": true, "zh_senses": [
        ["pos": "non-lemma form", "glosses": ["不该显示"]],
        ["glosses": ["小孩", "小孩", "child"], "examples": [["ja": "幼子を抱く。", "en": "hold a child"]]]],
    "examples": [["ja": "幼子を抱く。", "en": "duplicate"], ["ja": "子どもです。", "zh": "是孩子。"]],
    "inflect": ["surface": "幼な子", "base": "幼子", "variant": "spelling", "marks": ["异体"]],
    "kanji": [["kanji": "幼", "on": ["ヨウ"], "kun": ["おさない"], "meanings_zh": "年幼"]]]
let richEntry = try ReaderNativeWordLookup.entry(rich, word: "幼な子", japanese: true, mastered: true)
precondition(richEntry["meaning"] as? String == "小孩")
precondition(richEntry["reading"] as? String == "おさなご" && richEntry["accent"] as? Int == 3)
precondition(richEntry["inflect"] as? String == "写法 幼な子 词头 幼子 异体")
precondition((richEntry["examples"] as! [[String: Any]]).count == 2)
precondition((richEntry["examples"] as! [[String: Any]])[0]["zh"] as? String == "", "English is not Chinese")
precondition(richEntry["mastered"] as? Bool == true && richEntry["pos"] as? String == "")
let merged = ReaderNativeWordLookup.merge(local: rich, remote: ["ok": true, "stale": true, "source_word": "child", "source_lang": "en"])
precondition(merged["reading"] as? String == "おさなご" && merged["source_word"] as? String == "child")
precondition(!ReaderNativeWordLookup.cacheable(merged, japanese: true))
precondition(ReaderNativeWordLookup.meaning(["translation": "未能确定具体词义"]).isEmpty)
precondition(ReaderNativeWordLookup.meaning(["translation": "stem of 食べる"]).isEmpty)
precondition(ReaderNativeWordLookup.inflection(["lemma": "プライマリーヘルスケア"], word: "プライマリー・ヘルス・ケア", japanese: true).isEmpty)
precondition(ReaderNativeWordLookup.inflection(["lemma": "食べる", "inflect": ["marks": ["过去"]]], word: "食べた", japanese: true).contains("过去"))
let missingJP = try ReaderNativeWordLookup.entry(["ok": false], word: "語", japanese: true, mastered: false)
precondition(missingJP["missing"] as? Bool == true)
do { _ = try ReaderNativeWordLookup.entry(["ok": false], word: "missing", japanese: false, mastered: false); preconditionFailure("missing English entry accepted") } catch {}
var remoteReads = 0, fallbackReads = 0
_ = try await ReaderNativeWordLookup.lookup(japanese: true, local: { rich }, remote: {
    remoteReads += 1; return [:]
}, fallback: { _ in fallbackReads += 1; return nil })
precondition(remoteReads == 0 && fallbackReads == 0)
let enriched = try await ReaderNativeWordLookup.lookup(japanese: true, local: { ["ok": true, "reading": "ご", "jp": true] }, remote: {
    remoteReads += 1; return ["ok": true, "zh": "语言", "jp": true]
}, fallback: { _ in fallbackReads += 1; return nil })
precondition(enriched["reading"] as? String == "ご" && ReaderNativeWordLookup.meaning(enriched) == "语言")
precondition(remoteReads == 1 && fallbackReads == 0)
do {
    _ = try await ReaderNativeWordLookup.lookup(japanese: true, local: { throw CancellationError() }, remote: {
        remoteReads += 1; return [:]
    }, fallback: { _ in fallbackReads += 1; return nil })
    preconditionFailure("cancelled dictionary started network fallback")
} catch is CancellationError {}
precondition(remoteReads == 1 && fallbackReads == 0)
print("Native dictionary: complete rich fields, fallback merging, cache gates and cancellation passed")
