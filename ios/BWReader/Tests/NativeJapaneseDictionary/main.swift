import Foundation

let fixtureURL = URL(fileURLWithPath: CommandLine.arguments[1])
let root = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as! [String: Any]
var reads = [String: Int]()
let dictionary = ReaderNativeJapaneseDictionary { path in
    reads[path, default: 0] += 1
    return try Data(contentsOf: root.appendingPathComponent(path))
}
func equal(_ actual: Any, _ expected: Any, _ label: String) throws {
    let a = try JSONSerialization.data(withJSONObject: actual, options: [.sortedKeys, .fragmentsAllowed])
    let b = try JSONSerialization.data(withJSONObject: expected, options: [.sortedKeys, .fragmentsAllowed])
    guard a == b else {
        print("Mismatch \(label)\nactual: \(String(decoding:a,as:UTF8.self))\nexpected: \(String(decoding:b,as:UTF8.self))")
        exit(1)
    }
}
for test in fixture["candidates"] as! [[String: Any]] {
    let term = test["term"] as! String
    try equal(ReaderNativeJapaneseDictionary.candidateForms(term), test["expected"]!, "forms: " + term)
    precondition(ReaderNativeJapaneseDictionary.shardKey(term) == test["shard"] as! String, "shard: " + term)
    precondition(ReaderNativeJapaneseDictionary.moraCount(term) == test["mora"] as! Int, "mora: " + term)
}
for test in fixture["lookups"] as! [[String: Any]] {
    let term = test["term"] as! String, legacy = test["legacy"] as! Bool
    try equal(dictionary.lookup(term, legacy: legacy), test["expected"]!, "lookup \(legacy): " + term)
}
dictionary.clear(); reads.removeAll()
_ = try dictionary.lookup("日本語")
let first = reads
_ = try dictionary.lookup("日本語")
precondition(reads == first, "same lookup loaded dictionary files again")
dictionary.clear()
_ = try dictionary.lookup("日本語")
precondition(reads["manifest.json"] == 2, "cache invalidation did not reload installed files")
let invalid = ReaderNativeJapaneseDictionary { _ in Data(#"{"contract":"wrong"}"#.utf8) }
do { _ = try invalid.lookup("日本"); preconditionFailure("invalid manifest accepted") } catch {}
print("Native Japanese dictionary: real data, forms, variants, rich fields and cache ownership passed")
