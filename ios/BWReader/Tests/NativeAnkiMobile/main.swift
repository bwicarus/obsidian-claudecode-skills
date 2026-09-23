import Foundation

let store = try ReaderNativeDataStore(path: ":memory:")
var clock: Int64 = 1000
let repository = ReaderNativeCardRepository(store: store, deviceID: "native-mobile-test", now: { clock })
let mobile = ReaderNativeAnkiMobile(repository: repository, now: { clock })
let gid = "card_aced"
let nonce = String(repeating: "a", count: 32)
_ = try repository.perform(["operation": "registerDraft", "arguments": [["gid": gid, "cards": [
    ["type": "basic", "front": "日\n<b>本</b>", "back": "答え", "tags": ["日本語"]],
    ["type": "cloze", "cloze": "{{c1::答え}}"], ["type": "basic", "front": "未確認", "back": "答え"]],
    "source": ["kind": "test", "sourceId": "source"]]], "mutationId": "create"])
for index in 0...1 {
    _ = try repository.perform(["operation": "saveConfirmedCard", "arguments": [["gid": gid, "cardIndex": index]], "mutationId": "confirm-\(index)"])
}
do { _ = try mobile.prepare(gid: gid, index: 2); preconditionFailure("draft exported") } catch is ReaderNativeAnkiMobile.Failure {}
let prepared = try mobile.prepare(gid: gid, index: 0, nonce: nonce)
let pendingRows = try mobile.pending()
precondition(pendingRows.count == 1 && pendingRows[0].nonce == nonce)
let params = Dictionary(uniqueKeysWithValues: URLComponents(string: prepared.url)!.queryItems!.map { ($0.name, $0.value!) })
precondition(params["fldFront"] == "日<br><b>本</b>" && params["fldBack"] == "答え" && params["type"] == "Basic")
precondition(params["x-success"]!.contains("nonce=" + nonce))
precondition(params["tags"] == "bwreader bwgid_card_aced bwindex_0 日本語")
let cursor = try store.cursor()
do { _ = try mobile.prepare(gid: gid, index: 0); preconditionFailure("pending exported twice") } catch is ReaderNativeAnkiMobile.Failure {}
do { _ = try mobile.confirm(gid: gid, index: 0, nonce: String(repeating: "b", count: 32)); preconditionFailure("wrong callback confirmed") } catch is ReaderNativeAnkiMobile.Failure {}
let unchanged = try store.cursor(); precondition(unchanged == cursor)
// A verified callback is acknowledged only after the durable write succeeds.
try store.execute("CREATE TRIGGER fail_receipt BEFORE UPDATE ON records WHEN NEW.collection = 'card-states' AND NEW.json LIKE '%succeeded%' BEGIN SELECT RAISE(ABORT, 'forced receipt failure'); END")
do { _ = try mobile.confirm(gid: gid, index: 0, nonce: nonce); preconditionFailure("failed persistence acknowledged") } catch is ReaderNativeDataStore.StoreError {}
let retained = try mobile.pending(); precondition(retained.count == 1)
try store.execute("DROP TRIGGER fail_receipt")
_ = try mobile.confirm(gid: gid, index: 0, nonce: nonce)
let afterSuccess = try store.cursor()
_ = try mobile.confirm(gid: gid, index: 0, nonce: nonce)
let replayCursor = try store.cursor(); precondition(afterSuccess == replayCursor, "callback replay wrote twice")
do { _ = try mobile.prepare(gid: gid, index: 0); preconditionFailure("delivered card exported twice") } catch is ReaderNativeAnkiMobile.Failure {}
let second = try mobile.prepare(gid: gid, index: 1, nonce: String(repeating: "b", count: 32))
_ = try mobile.didNotOpen(second.pending, message: "not installed")
let retry = try mobile.prepare(gid: gid, index: 1, nonce: String(repeating: "c", count: 32))
clock = retry.pending.expiresAt
let recovered = ReaderNativeAnkiMobile(repository: repository, now: { clock })
let recoveredPending = try recovered.pending(); precondition(recoveredPending.count == 1)
_ = try recovered.expire(recoveredPending[0])
do { _ = try recovered.confirm(gid: gid, index: 1, nonce: retry.pending.nonce); preconditionFailure("expired callback accepted") } catch is ReaderNativeAnkiMobile.Failure {}
do { _ = try recovered.prepare(gid: gid, index: 1); preconditionFailure("unknown outcome retried") } catch is ReaderNativeAnkiMobile.Failure {}
do { _ = try ReaderNativeAnkiMobile.addURL(gid: gid, index: 0, card: ["type": "basic", "front": String(repeating: "語", count: 10000), "back": "答"], nonce: nonce); preconditionFailure("oversized external URL allowed") } catch is ReaderNativeAnkiMobile.Failure {}
print("Native AnkiMobile: durable-before-open, nonce correlation, rollback, replay, known-failure retry and unknown-result gate passed")
