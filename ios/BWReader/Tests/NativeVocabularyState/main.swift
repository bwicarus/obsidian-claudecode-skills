import Foundation
typealias V = ReaderNativeVocabularyState
typealias R = ReaderNativeCardRules
let fixtures = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
for item in fixtures["cases"] as! [[String: Any]] {
    do {
        let value = try V.normalized(item["input"] as! [String: Any])
        precondition(item["error"] == nil && R.same(value, item["result"]!), "vocabulary normalization differs: \(item), actual \(value)")
    } catch { precondition(item["error"] as? Bool == true, "unexpected normalization failure: \(error)") }
}
let store = try ReaderNativeDataStore(path: ":memory:")
let vocabulary = V(store: store, deviceID: "vocabulary-test", now: { 1000 })
for (index, item) in (fixtures["sequences"] as! [[String: Any]]).enumerated() {
    let input = item["input"] as! [String: Any], enabled = item["enabled"] as! Bool
    let value = try vocabulary.set(input, property: "mastered", enabled: enabled, mutation: "write-\(index)")
    precondition(R.same(value, item["result"]!), "vocabulary alias union differs")
    let cursor = try store.cursor()
    let replay = try vocabulary.set(input, property: "mastered", enabled: enabled, mutation: "write-\(index)")
    let replayCursor = try store.cursor()
    precondition(R.same(value, replay) && cursor == replayCursor, "retry wrote again")
    do {
        _ = try vocabulary.set(input, property: "mastered", enabled: !enabled, mutation: "write-\(index)")
        preconditionFailure("reused mutation accepted")
    } catch is V.Failure {}
}
let before = try store.records(collection: V.collection, idPrefix: ""), beforeCursor = try store.cursor()
try store.execute("CREATE TRIGGER fail_vocab BEFORE INSERT ON journal BEGIN SELECT RAISE(ABORT, 'forced journal failure'); END")
do { _ = try vocabulary.set(["key": "different"], property: "lookup", enabled: true, mutation: "failed"); preconditionFailure("partial vocabulary committed") }
catch is ReaderNativeDataStore.StoreError {}
let after = try store.records(collection: V.collection, idPrefix: ""), afterCursor = try store.cursor()
let receipt = try store.mutationResult(mutationId: "native-vocabulary:failed")
precondition(before.map(\.json) == after.map(\.json) && beforeCursor == afterCursor && receipt == nil)
try store.execute("DROP TRIGGER fail_vocab")
_ = try vocabulary.set(["key": "different"], property: "lookup", enabled: true, mutation: "failed")
let journal = try store.journal(after: 0, limit: 100)
let last = try JSONSerialization.jsonObject(with: Data(journal.last!.json.utf8)) as! [String: Any]
let envelope = last["record"] as! [String: Any]
precondition(last["operation"] as? String == "put" && last["mutationId"] as? String == "failed")
precondition((envelope["causal"] as! [String: Any])["parent"] is NSNull)
print("Native vocabulary: browser normalization, alias union, causal journal, retry and transactional rollback passed")
let phraseStore = try ReaderNativeDataStore(path: ":memory:")
let phraseVocabulary = V(store: phraseStore, deviceID: "phrase-test")
let phrase: [String: Any] = ["kind": "phrase", "language": "ja", "text": "予防接種を受ける"]
_ = try phraseVocabulary.set(phrase, property: "mastered", enabled: true, mutation: "phrase-on")
_ = try phraseVocabulary.set(["kind": "word", "language": "ja", "word": "接種"], property: "mastered", enabled: true, mutation: "word-on")
let withMastered = try phraseVocabulary.tokenizationPhrases(favorites: ["感染症予防法"])
precondition(withMastered == ["予防接種を受ける", "感染症予防法"])
let phraseMastered = try phraseVocabulary.enabled(phrase, property: "mastered")
let wordIsNotPhrase = try phraseVocabulary.enabled(["kind": "phrase", "language": "ja", "text": "接種"], property: "mastered")
precondition(phraseMastered && !wordIsNotPhrase)
_ = try phraseVocabulary.set(phrase, property: "mastered", enabled: false, mutation: "phrase-off")
let phraseUnmastered = try phraseVocabulary.enabled(phrase, property: "mastered")
precondition(!phraseUnmastered)
let withoutMastered = try phraseVocabulary.tokenizationPhrases(favorites: ["感染症予防法"])
let stillFavorite = try phraseVocabulary.tokenizationPhrases(favorites: ["予防接種を受ける"])
precondition(withoutMastered == ["感染症予防法"] && stillFavorite == ["予防接種を受ける"])
