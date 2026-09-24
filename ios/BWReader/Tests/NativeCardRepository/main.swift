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
let revealed = try ui.perform(command("reveal", "native-reveal"))["result"] as! [String: Any]
let revealedExact = ((revealed["states"] as! [String: Any])["0"] as! [String: Any])["exactState"] as! [String: Any]
precondition(revealedExact["_showBack"] as? Bool == true && revealedExact["_ratingUnavailable"] as? Bool == true)
precondition((revealed["cards"] as! [[String: Any]])[0]["back"] as? String == "答")

let queueStore = try ReaderNativeDataStore(path: ":memory:")
let queueRepo = ReaderNativeCardRepository(store: queueStore, deviceID: "queue", now: { 10000 })
for id in ["card_bbbb", "card_aaaa"] {
    _ = try queueRepo.perform(["operation": "registerDraft", "arguments": [["gid": id,
        "cards": (0..<7).map { ["type": "basic", "front": "\(id)-\($0)", "back": "答"] },
        "source": ["kind": "test", "documentId": "localbook:queue"]]], "mutationId": "draft-" + id])
    for index in 0..<6 {
        _ = try queueRepo.perform(["operation": "saveConfirmedCard", "arguments": [["gid": id, "cardIndex": index]], "mutationId": "confirm-\(id)-\(index)"])
    }
    for (index, patch) in [
        (0, ["review": ["status": "review", "dueAt": 9999]] as [String: Any]),
        (1, ["review": ["status": "review", "dueAt": 10001]]),
        (2, ["review": ["status": "suspended"]]),
        (3, ["flags": ["archived": true]]),
        (4, ["review": ["status": "review", "dueAt": 10000]])
    ] {
        _ = try queueRepo.perform(["operation": "patchState", "arguments": [id, index, patch], "mutationId": "state-\(id)-\(index)"])
    }
}
let queueCursor = try queueStore.cursor()
let prepared = try queueRepo.perform(["operation": "reviewQueue", "arguments": [["limit": 5]]])["result"] as! [String: Any]
let entries = prepared["entries"] as! [[String: Any]]
let identities = entries.map { (($0["record"] as! [String: Any])["id"] as! String) + ":" + String($0["cardIndex"] as! Int) }
precondition(identities == ["card_aaaa:0", "card_bbbb:0", "card_aaaa:4", "card_bbbb:4", "card_aaaa:5"])
precondition(prepared["hasLocalCards"] as? Bool == true && prepared["dueTotal"] as? Int == 4)
precondition(entries.last?["due"] as? Bool == false && (entries[0]["record"] as! [String: Any])["cards"] == nil)
let queueCursorAfter = try queueStore.cursor()
precondition(queueCursorAfter == queueCursor, "reading review queue wrote mutations")
do { _ = try queueRepo.reviewQueue(limit: 201); preconditionFailure("unbounded queue accepted") } catch is R.Failure {}
print("Native review queue: stable ordering, exact due boundary, states, limits and read-only transaction passed")

for item in fixture["schedules"] as! [[String: Any]] {
    let actual = try ReaderNativeCardRepository.scheduledReview(item["previous"] as! [String: Any],
        ease: item["ease"] as! Int, reviewedAt: (item["reviewedAt"] as! NSNumber).int64Value)
    precondition(R.same(actual, item["result"]!), "native scheduling differs from browser")
}
let ratingRecord = try ui.load(gid)!
let ratingInput: [String: Any] = ["gid": gid, "cardIndex": 0, "entityRev": ratingRecord["entityRev"]!,
    "stateRev": ratingRecord["stateRev"]!, "aid": "rating-a", "ease": 3, "reviewedAt": 6000,
    "file": "localbook:test", "ankiCardId": ""]
func rating(_ input: [String: Any], _ mutation: String) -> [String: Any] {
    ["operation": "commitReview", "arguments": [input], "mutationId": mutation]
}
let beforeRatingCursor = try localStore.cursor()
try localStore.execute("CREATE TRIGGER fail_rating BEFORE INSERT ON records WHEN NEW.collection = 'native-review-history' BEGIN SELECT RAISE(ABORT, 'forced history failure'); END")
do { _ = try ui.perform(rating(ratingInput, "review-a")); preconditionFailure("partial rating committed") }
catch is ReaderNativeDataStore.StoreError {}
let ratingRollback = try ui.load(gid)!, rollbackRatingCursor = try localStore.cursor()
precondition(R.same(ratingRollback, ratingRecord) && rollbackRatingCursor == beforeRatingCursor)
let failedRatingReceipt = try localStore.mutationResult(mutationId: "native-card-command:review-a")
precondition(failedRatingReceipt == nil)
try localStore.execute("DROP TRIGGER fail_rating")
let rated = try ui.perform(rating(ratingInput, "review-a"))["result"] as! [String: Any]
let ratedState = (rated["states"] as! [String: Any])["0"] as! [String: Any]
let schedule = ratedState["review"] as! [String: Any]
precondition((schedule["reps"] as! NSNumber).intValue == 1 && (schedule["dueAt"] as! NSNumber).int64Value == 86406000)
let ratingCursor = try localStore.cursor()
_ = try ui.perform(rating(ratingInput, "review-a"))
_ = try ui.perform(rating(ratingInput, "another-transport"))
let duplicateCursor = try localStore.cursor()
precondition(duplicateCursor == ratingCursor, "rating replay incremented schedule or journal")
let histories = try localStore.records(collection: "native-review-history", idPrefix: "")
precondition(histories.count == 1)
let journal = try localStore.journal(after: beforeRatingCursor, limit: 100)
precondition(!journal.contains { $0.json.contains("native-review-history") }, "local history leaked into sync journal")
var conflictingRating = ratingInput; conflictingRating["ease"] = 4
var staleRating = ratingInput; staleRating["aid"] = "stale-rating"
for (input, code) in [(conflictingRating, "MUTATION_REUSED"), (staleRating, "CONFLICT")] {
    do { _ = try ui.perform(rating(input, "reject-" + code)); preconditionFailure("invalid rating committed") }
    catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_" + code) }
}
var removedRating = ratingInput; removedRating["cardIndex"] = 1; removedRating["aid"] = "removed-rating"
do { _ = try ui.perform(rating(removedRating, "reject-removed")); preconditionFailure("removed card rated") }
catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_TRANSITION") }
var staleEntity = ratingInput
staleEntity["aid"] = "stale-entity"; staleEntity["entityRev"] = 999; staleEntity["stateRev"] = rated["stateRev"]!
do { _ = try ui.perform(rating(staleEntity, "reject-entity")); preconditionFailure("stale content rated") }
catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_CONFLICT") }
print("Native ratings: scheduling parity, atomic history, rollback, retry and revision fences passed")

// Refine only the committed review. A late response must not revert counters,
// a second rating, or an edited card, and replay retains the same journal rows.
let adoptionReview = ((rated["states"] as! [String: Any])["0"] as! [String: Any])["review"] as! [String: Any]
let adoptionInput: [String: Any] = ["gid": gid, "cardIndex": 0, "aid": "rating-a", "reviewedAt": 6000,
    "entityRev": rated["entityRev"]!, "expectedReview": adoptionReview, "next": ["interval": -600]]
func adopt(_ input: [String: Any], _ mutation: String) -> [String: Any] {
    ["operation": "adoptReviewSchedule", "arguments": [input], "mutationId": mutation]
}
let adopted = try ui.perform(adopt(adoptionInput, "native-interval"))["result"] as! [String: Any]
precondition(adopted["applied"] as? Bool == true)
let adoptedCard = adopted["record"] as! [String: Any]
let adoptedReview = ((adoptedCard["states"] as! [String: Any])["0"] as! [String: Any])["review"] as! [String: Any]
precondition((adoptedReview["dueAt"] as! NSNumber).int64Value == 606000)
precondition((adoptedReview["intervalDays"] as! NSNumber).doubleValue == 0.01)
precondition(adopted["scheduleSource"] as? String == "anki-fsrs" && adoptedReview["scheduleSource"] == nil)
for key in ["reps", "lapses", "ease", "lastReviewedAt", "status"] {
    precondition(R.same(adoptedReview[key] as Any, adoptionReview[key] as Any), "interval refinement rewrote " + key)
}
let adoptionCursor = try localStore.cursor()
_ = try ui.perform(adopt(adoptionInput, "native-interval"))
let adoptionReplayCursor = try localStore.cursor()
precondition(adoptionReplayCursor == adoptionCursor)
var newerRating = ratingInput
newerRating["aid"] = "newer-rating"; newerRating["reviewedAt"] = 7000; newerRating["ease"] = 1
newerRating["stateRev"] = adoptedCard["stateRev"]!
let newerRecord = try ui.perform(rating(newerRating, "newer-rating"))["result"] as! [String: Any]
let newerCursor = try localStore.cursor()
var lateInterval = adoptionInput; lateInterval["next"] = ["interval": 50]
let lateAdoption = try ui.perform(adopt(lateInterval, "late-native-interval"))["result"] as! [String: Any]
precondition(lateAdoption["applied"] as? Bool == false && lateAdoption["reason"] as? String == "stale")
let lateCursor = try localStore.cursor()
precondition(lateCursor == newerCursor)
let newestReview = ((newerRecord["states"] as! [String: Any])["0"] as! [String: Any])["review"] as! [String: Any]
var missingEvent = adoptionInput
missingEvent["expectedReview"] = newestReview; missingEvent["reviewedAt"] = 7000; missingEvent["aid"] = "unknown-rating"
do { _ = try ui.perform(adopt(missingEvent, "missing-event")); preconditionFailure("unproven interval written") }
catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_CONFLICT") }
print("Native Anki schedule refinement: seconds/days, counters, replay and stale rating fences passed")
var deliveryRating = newerRating
deliveryRating["aid"] = "delivery-rating"; deliveryRating["reviewedAt"] = 8000; deliveryRating["stateRev"] = newerRecord["stateRev"]!
let deliveryNamespace = "acct-v1-" + String(repeating: "a", count: 64)
let deliveryMutation = "mut-v2-" + String(repeating: "b", count: 32)
deliveryRating["delivery"] = [["contract": "command-outbox/2", "ownerNamespace": deliveryNamespace, "mutationId": deliveryMutation,
    "recordType": "mutation", "queueKey": "revlog:delivery-rating", "method": "POST", "url": "/pdf/api/review-event",
    "body": ["aid": "delivery-rating"], "ts": 8000]]
try localStore.execute("CREATE TRIGGER fail_delivery BEFORE INSERT ON records WHEN NEW.collection = 'native-review-delivery' BEGIN SELECT RAISE(ABORT, 'forced delivery failure'); END")
do { _ = try ui.perform(rating(deliveryRating, "delivery-rating")); preconditionFailure("score committed without its delivery record") }
catch is ReaderNativeDataStore.StoreError {}
let noDeliveryScore = try ui.load(gid)!
precondition(R.same(noDeliveryScore, newerRecord))
try localStore.execute("DROP TRIGGER fail_delivery")
_ = try ui.perform(rating(deliveryRating, "delivery-rating"))
let pendingDelivery = try ui.pendingReviewDeliveries(namespace: deliveryNamespace)
precondition(pendingDelivery.count == 1)
let wrongAccountDelivery = try ui.pendingReviewDeliveries(namespace: "another-account")
precondition(wrongAccountDelivery.isEmpty)
do { try ui.acknowledgeReviewDelivery(id: pendingDelivery[0].id, mutationID: "wrong"); preconditionFailure("wrong delivery acknowledged") }
catch let error as R.Failure { precondition(error.code == "BW_CARD_REPOSITORY_CONFLICT") }
try ui.acknowledgeReviewDelivery(id: pendingDelivery[0].id, mutationID: deliveryMutation)
_ = try ui.perform(rating(deliveryRating, "delivery-rating"))
let settledDelivery = try ui.pendingReviewDeliveries(namespace: deliveryNamespace)
precondition(settledDelivery.isEmpty, "score replay recreated an acknowledged delivery")

func testStandaloneRating() throws {
    let db = try ReaderNativeDataStore(path:":memory:")
    let repo = ReaderNativeCardRepository(store:db,deviceID:"test",now:{ 9000 })
    let gid = "card_standalone"
    _ = try repo.perform(["operation":"registerDraft","arguments":[["gid":gid,"cards":[["front":"Q","back":"A"]],"source":["kind":"test","sourceId":"source"]]],"mutationId":"new"])
    _ = try repo.perform(["operation":"saveConfirmedCard","arguments":[["gid":gid,"cardIndex":0]],"mutationId":"confirm"])
    _ = try repo.perform(["operation":"patchState","arguments":[gid,0,["exactState":["_st":"learn","_showBack":true,"card_id":123,"_ratingUnavailable":false]]],"mutationId":"ready"])
    func input() throws -> [String:Any] {
        let current = try repo.load(gid)!
        return ["gid":gid,"cardIndex":0,"entityRev":current["entityRev"]!,"stateRev":current["stateRev"]!]
    }
    let event:[String:Any] = ["contract":"command-outbox/2","url":"/pdf/api/review-event","body":["aid":"aid-one"],
        "ownerNamespace":deliveryNamespace,"mutationId":deliveryMutation,"recordType":"mutation","queueKey":"test","method":"POST","ts":9000]
    let prior = try repo.load(gid)!
    try db.execute("CREATE TRIGGER fail_sidebar_delivery BEFORE INSERT ON records WHEN NEW.collection = 'native-review-delivery' BEGIN SELECT RAISE(ABORT, 'forced event failure'); END")
    do { _ = try repo.beginStandaloneRating(input:input(),aid:"aid-one",ease:3,event:event); preconditionFailure("pending score without event") } catch is ReaderNativeDataStore.StoreError {}
    let rolled = try repo.load(gid)!
    precondition(R.same(prior,rolled))
    try db.execute("DROP TRIGGER fail_sidebar_delivery")
    let started = try repo.beginStandaloneRating(input:input(),aid:"aid-one",ease:3,event:event)
    precondition((started["body"] as? [String:Any])?["card_id"] as? Int == 123)
    do { _ = try repo.beginStandaloneRating(input:input(),aid:"aid-two",ease:3,event:event); preconditionFailure("duplicate rating allowed") } catch is R.Failure {}
    let unknown = try repo.settleStandaloneRating(started,status:"unknown")
    precondition((((unknown["states"] as? [String:Any])?["0"] as? [String:Any])?["exactState"] as? [String:Any])?["_ratingPending"] as? Bool == true)
    let accepted = try repo.settleStandaloneRating(started,status:"succeeded",next:["interval":5])
    let exact = ((accepted["states"] as! [String:Any])["0"] as! [String:Any])["exactState"] as! [String:Any]
    precondition(exact["_ratingPending"] as? Bool == false && (exact["_next"] as? [String:Any])?["interval"] as? Int == 5)
    do { _ = try repo.settleStandaloneRating(started,status:"rejected"); preconditionFailure("late failure rolled back accepted score") } catch is R.Failure {}
}
try testStandaloneRating()
print("Standalone native rating: atomic event/state, concurrent rejection, unknown reservation and original receipt passed")
