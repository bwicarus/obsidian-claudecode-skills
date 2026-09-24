import Foundation

typealias P = ReaderNativePhraseStore
typealias R = ReaderNativeCardRules
let root = FileManager.default.temporaryDirectory.appendingPathComponent("native-phrase-test-" + UUID().uuidString)
try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: root) }
let path = root.appendingPathComponent("device.sqlite").path
let store = try ReaderNativeDataStore(path: path)
let phrases = P(store: store, deviceID: "device-test")
do { _ = try phrases.read(); preconditionFailure("read before import") } catch {}
try store.putMeta("legacyImport", json: "done")
let empty = try phrases.read()
precondition(!empty.seeded && empty.revision == 0)
do { _ = try phrases.set("食中毒", enabled: true); preconditionFailure("edit overwrote unread history") } catch {}
let seed = try phrases.seed([" 食\n中 毒　", "食中毒", "歴史", " "])
precondition(seed.phrases == ["食中毒", "歴史"] && seed.revision == 1 && seed.seeded)
let secondSeed = try phrases.seed(["wrong"])
precondition(secondSeed.phrases == seed.phrases)
let firstEdit = try phrases.set("再\u{feff}興\u{a0}感染症", enabled: true)
let replay = try phrases.set("再興感染症", enabled: true)
precondition(firstEdit.revision == 2 && replay.revision == 2)
_ = try phrases.set("食中毒", enabled: false)
let latest = try phrases.set("食中毒", enabled: true)
let pending = try phrases.pendingEffects()!
let changes = pending["changes"] as! [[String: Any]]
precondition(changes.count == 2 && changes.last!["enabled"] as? Bool == true)
try phrases.completeEffects(revision: firstEdit.revision)
let stillPending = try phrases.pendingEffects()
precondition(stillPending != nil, "late mirror ack removed a later edit")

// Failure after the list write must roll back both its journal and value.
let cursor = try store.cursor()
try store.putMeta("native-phrase-effects:device-test", json: "invalid")
do { _ = try phrases.set("失敗", enabled: true); preconditionFailure("partial commit") } catch {}
let rolledBack = try phrases.read(), rolledBackCursor = try store.cursor()
precondition(rolledBack.phrases == latest.phrases && rolledBack.revision == latest.revision && cursor == rolledBackCursor)
try store.putMeta("native-phrase-effects:device-test", json: String(decoding: R.bytes(pending), as: UTF8.self))
store.close()
let reopened = try ReaderNativeDataStore(path: path)
let restored = P(store: reopened, deviceID: "device-test")
let durable = try restored.pendingEffects()!
precondition(R.same(durable, pending), "pending effects lost on restart")
try restored.completeEffects(revision: latest.revision)
let cleared = try restored.pendingEffects()
precondition(cleared == nil)
for text in ["", "\0", String(repeating:"😀", count:33)] {
    do { _ = try restored.set(text, enabled:true); preconditionFailure("invalid phrase accepted") } catch {}
}
let envelope = try JSONSerialization.jsonObject(with: Data(reopened.record(collection:P.collection,id:"device-test:phrase-favorites")!.json.utf8)) as! [String:Any]
precondition(envelope["schema"] as? Int == 1 && envelope["deleted"] as? Bool == false)
let value = envelope["value"] as! [String:Any]
precondition(value["id"] as? String == "device-test:phrase-favorites" && value["deviceId"] as? String == "device-test")
reopened.close()
print("Native phrases: original envelope, seeded history, idempotence, durable/coalesced effects, late ack and atomic rollback passed")
