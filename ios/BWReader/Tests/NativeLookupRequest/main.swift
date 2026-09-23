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
