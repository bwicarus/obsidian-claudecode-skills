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
        let actual = try rows.map { try JSONSerialization.jsonObject(with: Data($0.json.utf8)) }
        precondition(R.same(actual, item["records"]!), "persisted rows differ: \(item["operation"]!)\nactual \(actual)\nexpected \(item["records"]!)")
        count += 1
    }
}
print("Native card repository: \(count) browser parity cases passed")
