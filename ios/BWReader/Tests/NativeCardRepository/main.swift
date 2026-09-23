import Foundation

typealias R = ReaderNativeCardRules
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
var count = 0
for item in fixture["cases"] as! [[String: Any]] {
    do {
        let result: Any
        switch item["operation"] as! String {
        case "normalizeCard": result = try R.card(item["input"])
        case "normalizeCards": result = try R.cards(item["input"])
        default: result = try R.source(item["input"])
        }
        precondition(item["error"] == nil && R.same(result, item["result"]!), "normalization differs: \(item["operation"]!)")
    } catch let error as R.Failure {
        precondition(error.code == item["error"] as? String, "normalization error differs: \(error) expected \(String(describing:item["error"]))")
    }
    count += 1
}
for sequence in fixture["sequences"] as! [[[String: Any]]] {
    let store = try ReaderNativeDataStore(path: ":memory:")
    for item in sequence {
        let stamp = (item["at"] as! NSNumber).int64Value
        let repository = ReaderNativeCardRepository(store: store, deviceID: "fixture", now: { stamp })
        do {
            let reply = try repository.perform(item)
            precondition(item["error"] == nil && R.same(reply["result"]!, item["result"]!), "operation differs: \(item["operation"]!)\nactual \(reply)\nexpected \(item)")
            if !["load", "snapshot"].contains(item["operation"] as! String) {
                let cursor = try store.cursor(), replay = try repository.perform(item)
                precondition(R.same(replay["result"]!, reply["result"]!) && replay["replayed"] as? Bool == true)
                let after = try store.cursor()
                precondition(after == cursor, "replay wrote again")
            }
        } catch let error as R.Failure {
            precondition(error.code == item["error"] as? String, "operation error differs: \(item["operation"]!) \(error) expected \(String(describing:item["error"]))")
        }
        let rows = try store.records(collection: "card-entities", idPrefix: "") + store.records(collection: "card-states", idPrefix: "")
        // Storage enumeration order is not part of the record contract (the
        // browser memory store re-inserts a tombstone; SQLite orders by id).
        func ordered(_ values: [[String: Any]]) -> [[String: Any]] {
            values.sorted {
                let a = ($0["collection"] as! String) + "/" + ($0["id"] as! String)
                let b = ($1["collection"] as! String) + "/" + ($1["id"] as! String)
                return a < b
            }
        }
        let actual = try ordered(rows.map { try JSONSerialization.jsonObject(with: Data($0.json.utf8)) as! [String: Any] })
        let expected = ordered(item["records"] as! [[String: Any]])
        precondition(R.same(actual, expected), "persisted rows differ: \(item["operation"]!)\nactual \(actual)\nexpected \(expected)")
        count += 1
    }
}
print("Native card repository: \(count) browser parity cases passed")

// UI commands run without a web owner, retain stable slots and all writes /
// retry receipts roll back together if the second stage of confirmation fails.
let localStore = try ReaderNativeDataStore(path: ":memory:")
let ui = ReaderNativeCardRepository(store: localStore, deviceID: "native-ui", now: { 5000 })
let gid = "card_cafe"
_ = try ui.perform(["operation": "registerDraft", "arguments": [["gid": gid,
    "cards": [["type": "basic", "front": "題", "back": "答"], ["type": "basic", "front": "次", "back": "次答"]],
    "source": ["kind": "test", "sourceId": "source"]]], "mutationId": "create-ui"])
func command(_ action: String, _ mutation: String, fields: [String: Any] = [:]) throws -> [String: Any] {
    let current = try ui.load(gid)!
    var input: [String: Any] = ["gid": gid, "cardIndex": 0, "action": action,
        "entityRev": current["entityRev"]!, "stateRev": current["stateRev"]!]
    input.merge(fields) { _, value in value }
    return ["operation": "interact", "arguments": [input], "mutationId": mutation]
}
let stale = try command("edit", "stale", fields: ["field": "front", "text": "old"])
_ = try ui.perform(command("edit", "edit", fields: ["field": "front", "text": "新しい題"] ))
do { _ = try ui.perform(stale); preconditionFailure("stale UI wrote over an edit") }
catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_CONFLICT") }
let beforeSave = try ui.load(gid)!, beforeCursor = try localStore.cursor()
try localStore.execute("CREATE TRIGGER fail_confirmation BEFORE UPDATE ON records WHEN NEW.collection = 'card-states' AND NEW.json LIKE '%learn%' BEGIN SELECT RAISE(ABORT, 'forced state projection failure'); END")
let confirmation = try command("add", "confirm")
do { _ = try ui.perform(confirmation); preconditionFailure("injected disk failure ignored") }
catch is ReaderNativeDataStore.StoreError { }
let rolledBack = try ui.load(gid)!, rolledCursor = try localStore.cursor()
precondition(R.same(beforeSave, rolledBack) && beforeCursor == rolledCursor, "partial confirmation escaped rollback")
let abandonedReceipt = try localStore.mutationResult(mutationId: "native-card-command:confirm")
precondition(abandonedReceipt == nil)
try localStore.execute("DROP TRIGGER fail_confirmation")
let confirmed = try ui.perform(confirmation)["result"] as! [String: Any]
precondition((confirmed["cards"] as! [[String: Any]])[0]["front"] as? String == "新しい題")
let confirmedState = (confirmed["states"] as! [String: Any])["0"] as! [String: Any]
precondition(confirmedState["phase"] as? String == "confirmed")
precondition((confirmedState["exactState"] as! [String: Any])["_st"] as? String == "learn")
let savedCursor = try localStore.cursor(), repeated = try ui.perform(confirmation), afterRepeat = try localStore.cursor()
precondition(savedCursor == afterRepeat && repeated["replayed"] as? Bool == true)
do { _ = try ui.perform(command("edit", "edit-confirmed", fields: ["field": "front", "text": "bad"])); preconditionFailure("confirmed card edited as draft") }
catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_TRANSITION") }
_ = try ui.perform(command("del", "remove-second", fields: ["cardIndex": 1]))
let removed = try ui.load(gid)!
precondition((removed["cards"] as! [Any]).count == 2)
precondition(((removed["states"] as! [String: Any])["1"] as! [String: Any])["removed"] as? Bool == true)
print("Native card UI: edit, confirm, revision fence, replay, deletion and transactional rollback passed")
