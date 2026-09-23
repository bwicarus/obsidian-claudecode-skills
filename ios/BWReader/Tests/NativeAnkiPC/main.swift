import Foundation

let store = try ReaderNativeDataStore(path: ":memory:")
let repo = ReaderNativeCardRepository(store: store, deviceID: "native-export-test", now: { 1000 })
let owner = ReaderNativeAnkiPC(repository: repo, now: { 1000 })
let gid = "card_abba"
_ = try repo.perform(["operation": "registerDraft", "arguments": [["gid": gid,
    "cards": [["type": "basic", "front": "日本", "back": "Japan", "tags": ["test"]], ["type": "cloze", "cloze": "{{c1::答}}"], ["type": "basic", "front": "未確認", "back": "答"]],
    "source": ["kind": "test", "documentId": "localbook:source", "location": ["page": 45], "quote": "日本", "kjTrack": "jp-word"]]], "mutationId": "create"])
for index in 0...1 { _ = try repo.perform(["operation": "saveConfirmedCard", "arguments": [["gid": gid, "cardIndex": index]], "mutationId": "confirm-\(index)"]) }
do { _ = try owner.prepare(gid: gid, index: 2, render: { $0 }); preconditionFailure("draft exported") } catch is ReaderNativeAnkiPC.Failure {}
// If the durable write fails, no transport request can be obtained by the caller.
try store.execute("CREATE TRIGGER fail_pending BEFORE UPDATE ON records WHEN NEW.collection = 'card-states' AND NEW.json LIKE '%pending%' BEGIN SELECT RAISE(ABORT, 'forced pending failure'); END")
do { _ = try owner.prepare(gid: gid, index: 0, render: { $0 }); preconditionFailure("pending failure ignored") } catch is ReaderNativeDataStore.StoreError {}
try store.execute("DROP TRIGGER fail_pending")
let attempt = try owner.prepare(gid: gid, index: 0, render: { "<p>" + $0 + "</p>" })
let request = try JSONSerialization.jsonObject(with: attempt.request) as! [String: Any]
precondition(request["entityId"] as? String == gid && request["track"] as? String == "jp-word")
precondition((request["projection"] as? [String: Any])?["front"] as? String == "<p>日本</p>")
precondition((request["card"] as? [String: Any])?["front"] as? String == "日本")
precondition((request["cards"] as? [[String: Any]])?.count == 3 && (request["card"] as? [String: Any])?["tags"] == nil)
precondition((request["target"] as? [String: Any])?["page"] as? Int == 45)
do { _ = try owner.prepare(gid: gid, index: 0, render: { $0 }); preconditionFailure("pending duplicate") } catch is ReaderNativeAnkiPC.Failure {}
_ = try owner.settle(attempt, result: nil, errorCode: "network", sent: false)
let retry = try owner.prepare(gid: gid, index: 0, render: { $0 })
precondition(retry.aid == attempt.aid, "safe retry must retain original AID")
let malformed = Data(#"{"ok":true,"added":1,"note_ids":[true],"card_ids":[],"card_ids_by_note":{}}"#.utf8)
do { _ = try owner.settle(retry, result: malformed); preconditionFailure("boolean accepted as note ID") } catch is ReaderNativeAnkiPC.Failure {}
_ = try owner.settle(retry, result: nil, errorCode: "BW_READER_LOCAL_ANKI_RESPONSE_INVALID")
do { _ = try owner.prepare(gid: gid, index: 0, render: { $0 }); preconditionFailure("unknown duplicate") } catch is ReaderNativeAnkiPC.Failure {}
let second = try owner.prepare(gid: gid, index: 1, render: { $0 })
let success = Data(#"{"ok":true,"added":1,"note_ids":[1711111111111],"card_ids":[1711111111112],"card_ids_by_note":{"1711111111111":[1711111111112]}}"#.utf8)
_ = try owner.settle(second, result: success)
do { _ = try owner.prepare(gid: gid, index: 1, render: { $0 }); preconditionFailure("successful duplicate") } catch is ReaderNativeAnkiPC.Failure {}
_ = try repo.perform(["operation": "saveConfirmedCard", "arguments": [["gid": gid, "cardIndex": 2]], "mutationId": "confirm-2"])
_ = try owner.prepare(gid: gid, index: 2, render: { $0 })
let recovered = try owner.recoverInterrupted()
precondition(recovered.count == 1 && ReaderNativeAnkiPC.receipt(recovered[0], index: 2)["status"] as? String == "unknown")
do { _ = try owner.prepare(gid: gid, index: 2, render: { $0 }); preconditionFailure("restart retried pending") } catch is ReaderNativeAnkiPC.Failure {}
print("Native desktop Anki: source identity, durable-before-send, stable AID, response validation and unknown-result gates passed")
