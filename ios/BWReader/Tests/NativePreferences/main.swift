import Foundation
typealias P = ReaderNativePreferences
typealias R = ReaderNativeCardRules
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
let catalog = try P.Catalog(data: R.bytes(fixture["catalog"]!))
let global = try ReaderNativeDataStore(path: ":memory:")
let device = try ReaderNativeDataStore(path: ":memory:")
for item in fixture["cases"] as! [[String: Any]] {
    let entry = try catalog.entry(item["key"] as! String)
    let store = entry.collection == "user-settings" ? global : device
    let preferences = P(store: store, deviceID: "prefs-test", now: { 1234 })
    let raw = item["raw"] as? String, mutation = item["mutation"] as! String
    let reply = try preferences.commit(entry, raw: raw, mutation: mutation)
    precondition(R.same(reply["result"]!, item["record"]!), "browser/native setting envelope differs: \(item["key"]!)")
    let read = try preferences.raw(entry)
    precondition(read == raw, "native read differs")
    let before = try store.cursor()
    let replay = try preferences.commit(entry, raw: raw, mutation: mutation)
    let after = try store.cursor()
    precondition(R.same(replay["result"]!, reply["result"]!) && before == after, "retry writes twice")
    do { _ = try preferences.commit(entry, raw: "different", mutation: mutation); preconditionFailure("reused mutation accepted") }
    catch is P.Failure {}
}
let entry = try catalog.entry("pdf-vocab-underline")
let p = P(store: global, deviceID: "prefs-test", now: { 1234 })
let old = try p.record(entry)!, cursor = try global.cursor()
do { _ = try p.commit(entry, raw: "1", mutation: "old-revision", expectedRevision: 0); preconditionFailure("CAS bypass") }
catch is ReaderNativeDataStore.StoreError {}
try global.execute("CREATE TRIGGER fail_pref BEFORE INSERT ON journal BEGIN SELECT RAISE(ABORT, 'journal failure'); END")
do { _ = try p.commit(entry, raw: "1", mutation: "rollback"); preconditionFailure("journal failure accepted") }
catch is ReaderNativeDataStore.StoreError {}
let after = try p.record(entry)!, afterCursor = try global.cursor()
let receipt = try global.mutationResult(mutationId: "native-preference:rollback")
precondition(R.same(old, after) && cursor == afterCursor && receipt == nil, "partial settings transaction")
try global.execute("DROP TRIGGER fail_pref")
_ = try p.commit(entry, raw: "1", mutation: "rollback")
do { _ = try catalog.entry("pdf-not-registered"); preconditionFailure("unknown setting accepted") }
catch is P.Failure {}
do { _ = try p.commit(entry, raw: String(repeating: "あ", count: 21846), mutation: "oversize"); preconditionFailure("oversize setting accepted") }
catch is P.Failure {}
// Corruption must be visible, never treated as a missing/default setting.
try global.execute("UPDATE records SET json='{}' WHERE collection='user-settings'")
do { _ = try p.raw(entry); preconditionFailure("corrupt record treated as default") }
catch {}
print("Native preferences: 55 keys, browser envelope parity, scope isolation, CAS, replay, rollback and corruption checks passed")
