import Foundation

typealias Q = ReaderNativeReviewQueue
@MainActor final class Fixture {
    var cache: String?
    var local: Q.Object = ["hasLocalCards": false, "entries": [], "dueTotal": 0]
    var failLocal = false, failSave = false, failPost = false, failGet = false, hold = false
    var held: CheckedContinuation<Void, Never>?
    var calls: [(String, Q.Object)] = []
    var reply: Q.Object = ["ok": true, "cards": [["id": 7, "question": "問", "answer": "答"]], "due_total": 3]
    var stamp = 10_000_000.0
    var status = 200
    var transportError: Error?
    var receipts: [String: String] = [:]
    var failReceipt = false
    var removedInputs: [Q.Object] = []
    var removedRecord: Q.Object = [:]
    var scoreCalls = 0, holdScore = false, failAfterScore = false
    var heldScore: CheckedContinuation<Void, Never>?
    var scoreResult: Q.Object = ["ok": true]
    lazy var service = Q(local: { [self] in
        if failLocal { throw Q.Failure(message: "repository unavailable") }; return local
    }, read: { [self] in cache }, write: { [self] text in
        if failSave { throw Q.Failure(message: "disk full") }; cache = text
    }, fetch: { [self] path, method, data in
        calls.append((method, data.isEmpty ? [:] : try JSONSerialization.jsonObject(with: data) as! Q.Object))
        let value = reply
        if hold { hold = false; await withCheckedContinuation { held = $0 } }
        if method == "POST" ? failPost : failGet { throw Q.Failure(message: "offline") }
        if let transportError { throw transportError }
        return .init(status: status, data: try JSONSerialization.data(withJSONObject: value))
    }, now: { [self] in stamp }, removeLocal: { [self] input in
        removedInputs.append(input); return removedRecord
    }, readReceipt: { [self] in receipts[$0] }, writeReceipt: { [self] key, value in
        if failReceipt { throw Q.Failure(message: "receipt disk full") }; receipts[key] = value
    }, score: { [self] stage in
        scoreCalls += 1
        precondition((stage["reviewedAt"] as? NSNumber)?.doubleValue == floor(stamp))
        if holdScore { holdScore = false; await withCheckedContinuation { heldScore = $0 } }
        if failAfterScore { failSave = true }
        return scoreResult
    })
    func input(scope: String = "current", page: Int = 4, force: Bool = true) -> Q.Object {
        ["request": UUID().uuidString, "contextKey": "ctx-book-\(page)", "context": ["file": "localbook:book", "page": page], "scope": scope, "force": force]
    }
}

@main struct Tests {
    @MainActor static func main() async throws {
        let context = ReaderNativeReviewCards.context(["file_rel":"localbook:book", "page":4,
            "selection":"接種", "visible_text":"原文\n\"quoted\"", "ignored":"not in queue context"])
        let contextKey = try ReaderNativeReviewCards.contextKey(context)
        precondition(contextKey == "ctx-3b75acae", "native context broke the existing UTF-16 ordered JSON cache key")
        precondition(context["ignored"] == nil)
        let local = Fixture(); local.local = ["hasLocalCards": true, "entries": [], "dueTotal": 0]
        let empty = try await local.service.load(local.input())
        precondition(empty["kind"] as? String == "local" && local.calls.isEmpty)
        local.failLocal = true
        do { _ = try await local.service.load(local.input()); preconditionFailure("local failure fell through") } catch {}
        precondition(local.calls.isEmpty)

        let remote = Fixture()
        remote.reply["related_total"] = 0
        let current = try await remote.service.load(remote.input())
        precondition(((current["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).isEmpty)
        precondition(remote.calls[0].0 == "POST")
        let all = try await remote.service.load(remote.input(scope: "all", force: false))
        precondition(remote.calls.count == 2 && remote.calls[1].0 == "GET")
        precondition(((all["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).count == 1)
        remote.failGet = true
        let cached = try await remote.service.load(remote.input(scope: "all", force: false))
        precondition(cached["kind"] as? String == "cache" && remote.calls.count == 2)

        let fallback = Fixture(); fallback.failPost = true
        let due = try await fallback.service.load(fallback.input())
        precondition(due["kind"] as? String == "fallback" && fallback.calls.map(\.0) == ["POST", "GET"])
        fallback.failGet = true
        let offline = try await fallback.service.load(fallback.input())
        precondition(offline["kind"] as? String == "offline")
        do { _ = try await fallback.service.load(fallback.input(page: 9)); preconditionFailure("other page cache leaked") } catch {}

        let legacy = Fixture()
        var old = legacy.input(force: false)
        old["legacyCache"] = ["client_context_key": "ctx-book-4", "ts": legacy.stamp - 100,
            "cards": [["id": 8, "question": "cached"]], "index": 0, "due_total": 0, "related_total": 0, "completed_ids": [1, 2]]
        old["rejectedIds"] = ["2"]
        let imported = try await legacy.service.load(old)
        let importedSnapshot = imported["snapshot"] as! Q.Object
        precondition(imported["kind"] as? String == "cache" && legacy.calls.isEmpty && legacy.cache != nil)
        precondition((importedSnapshot["completed_ids"] as! [NSNumber]).map(\.intValue) == [1])
        precondition(importedSnapshot["due_total"] as? Int == 0)
        let prior = legacy.cache
        let staleSave = try legacy.service.save(importedSnapshot, request: UUID().uuidString)
        precondition(!staleSave && prior == legacy.cache)

        let disk = Fixture(); disk.failSave = true
        do { _ = try await disk.service.load(disk.input()); preconditionFailure("disk failure hidden") }
        catch { precondition(error.localizedDescription == "disk full") }
        precondition(disk.calls.count == 1 && disk.cache == nil)

        let delayed = Fixture(); delayed.hold = true
        let earlier = delayed.input()
        let loading = Task { try await delayed.service.load(earlier) }
        while delayed.held == nil { await Task.yield() }
        let latest = try await delayed.service.load(delayed.input(page: 5))
        let retained = delayed.cache
        delayed.held?.resume(); delayed.held = nil
        do { _ = try await loading.value; preconditionFailure("stale load published") } catch {}
        precondition(delayed.cache == retained && (latest["snapshot"] as! Q.Object)["client_context_key"] as? String == "ctx-book-5")

        let cancelled = Fixture(); cancelled.hold = true
        let request = cancelled.input()
        let pending = Task { try await cancelled.service.load(request) }
        while cancelled.held == nil { await Task.yield() }
        cancelled.service.cancel(request["request"] as! String)
        cancelled.held?.resume(); cancelled.held = nil
        do { _ = try await pending.value; preconditionFailure("cancelled load published") } catch {}
        precondition(cancelled.cache == nil && cancelled.calls.count == 1)

        let arrived = Fixture(); arrived.hold = true
        let wait = Task { try await arrived.service.load(arrived.input()) }
        while arrived.held == nil { await Task.yield() }
        arrived.local = ["hasLocalCards": true, "entries": [], "dueTotal": 0]
        arrived.held?.resume(); arrived.held = nil
        let replacement = try await wait.value
        precondition(replacement["kind"] as? String == "local" && arrived.cache != nil)
        let scoring = Fixture()
        let navigation = Fixture()
        navigation.reply["cards"] = [["id": 7, "question": "one"], ["id": 8, "question": "two"]]
        let navigationInput = navigation.input(scope: "all")
        let navigationLoaded = try await navigation.service.load(navigationInput)
        let navigationSnapshot = navigationLoaded["snapshot"] as! Q.Object
        let navigationCards = navigationSnapshot["cards"] as! [Q.Object]
        let navigationRequest: Q.Object = ["lease": navigationInput["request"]!, "snapshot": navigationSnapshot,
            "current": navigationCards[0], "target": navigationCards[1]]
        let beforeNavigation = navigation.cache
        navigation.failSave = true
        do { _ = try navigation.service.selectCard(navigationRequest); preconditionFailure("failed selection saved") } catch {}
        precondition(navigation.cache == beforeNavigation)
        navigation.failSave = false
        let selection = try navigation.service.selectCard(navigationRequest)
        let selectedSnapshot = selection["snapshot"] as! Q.Object
        precondition(selection["changed"] as? Bool == true && selectedSnapshot["index"] as? Int == 1)
        precondition(ReaderNativeCardRules.same(navigationSnapshot["cards"]!, selectedSnapshot["cards"]!))
        var staleTarget = navigationRequest; staleTarget["target"] = ["id": 8, "question": "changed"]
        do { _ = try navigation.service.selectCard(staleTarget); preconditionFailure("changed card selected") } catch {}
        let navigationLease = navigationInput["request"] as! String
        let firstID = ReaderNativeReviewCards.stableID(navigationCards[0]), secondID = ReaderNativeReviewCards.stableID(navigationCards[1])
        do { _ = try navigation.service.navigate(lease:navigationLease,currentID:firstID,targetID:secondID); preconditionFailure("old native button changed the queue") } catch {}
        navigation.failSave = true
        let savedNavigation = navigation.cache
        do { _ = try navigation.service.navigate(lease:navigationLease,currentID:secondID,targetID:firstID); preconditionFailure("native navigation ignored disk failure") } catch {}
        precondition(navigation.cache == savedNavigation)
        navigation.failSave = false
        let byID = try navigation.service.navigate(lease:navigationLease,currentID:secondID,targetID:firstID)
        precondition((byID["snapshot"] as? Q.Object)?["index"] as? Int == 0)
        do { _ = try navigation.service.navigate(lease:navigationLease,currentID:firstID,targetID:"missing"); preconditionFailure("unknown card selected") } catch {}
        navigation.service.invalidate()
        do { _ = try navigation.service.selectCard(navigationRequest); preconditionFailure("stale scope selected") } catch {}
        let nativeScoring = Fixture()
        let nativeLoad = try await nativeScoring.service.load(nativeScoring.input(scope:"all"))
        let nativeLease = nativeLoad["request"] as! String
        let nativeCard = (nativeLoad["snapshot"] as! Q.Object)["cards"] as! [Q.Object]
        let nativeID = ReaderNativeReviewCards.stableID(nativeCard[0])
        let nativeStage = UUID().uuidString, nativeBefore = nativeScoring.cache
        do { _ = try nativeScoring.service.stageCurrentRating(lease:nativeLease,cardID:nativeID,stageID:nativeStage,ease:3); preconditionFailure("hidden answer rated") } catch {}
        _ = try nativeScoring.service.interact(["lease":nativeLease,"cardId":nativeID,"key":"reveal"])
        let nativeRated = try nativeScoring.service.stageCurrentRating(lease:nativeLease,cardID:nativeID,stageID:nativeStage,ease:3)
        precondition(nativeScoring.cache == nativeBefore && nativeScoring.calls.count == 1)
        precondition(((nativeRated["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).isEmpty)
        try nativeScoring.service.validateVisibleCard(lease:nativeLease,cardID:"")
        nativeScoring.failSave = true
        do { _ = try nativeScoring.service.undoCurrentRating(lease:nativeLease,cardID:""); preconditionFailure("failed undo accepted") } catch {}
        nativeScoring.failSave = false
        let nativeUndone = try nativeScoring.service.undoCurrentRating(lease:nativeLease,cardID:"")
        precondition(((nativeUndone["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).count == 1)
        precondition(nativeScoring.calls.count == 1,"staging/undo called external scheduler")
        let loaded = try await scoring.service.load(scoring.input())
        let scoreLease = loaded["request"] as! String
        var scoreSnapshot = loaded["snapshot"] as! Q.Object
        let olderIDs = Array(100...199)
        scoreSnapshot["completed_ids"] = olderIDs
        _ = try scoring.service.save(scoreSnapshot, request: scoreLease)
        let beforeStage = scoring.cache
        let stageID = UUID().uuidString
        let card = (scoreSnapshot["cards"] as! [Q.Object])[0]
        let stageRequest: Q.Object = ["lease": scoreLease, "stageId": stageID,
            "snapshot": scoreSnapshot, "card": card, "cardKey": "anki_card_7", "ease": 3, "revealed": true]
        var invalid = stageRequest; invalid["revealed"] = false
        do { _ = try scoring.service.stageRating(invalid); preconditionFailure("hidden answer rated") } catch {}
        do { _ = try scoring.service.stageRating(stageRequest); preconditionFailure("caller revealed the answer without native state") } catch {}
        _ = try scoring.service.interact(["lease": scoreLease, "cardId": "anki_card_7", "key": "reveal"])
        let staged = try scoring.service.stageRating(stageRequest)
        let shortened = staged["snapshot"] as! Q.Object
        precondition((shortened["cards"] as! [Q.Object]).isEmpty && scoring.cache == beforeStage)
        do { _ = try scoring.service.stageRating(stageRequest); preconditionFailure("stage duplicated") } catch {}
        scoring.service.discardRating(lease: scoreLease, stageID: UUID().uuidString)
        let undo: Q.Object = ["lease": scoreLease, "stageId": stageID, "snapshot": shortened]
        scoring.failSave = true
        do { _ = try scoring.service.undoRating(undo); preconditionFailure("failed undo silently accepted") } catch {}
        scoring.failSave = false
        let restored = try scoring.service.undoRating(undo)["snapshot"] as! Q.Object
        precondition((restored["cards"] as! [Q.Object]).count == 1)
        precondition((restored["completed_ids"] as! [NSNumber]).map(\.intValue) == olderIDs)
        precondition(scoring.calls.count == 1, "undo must not call an external scheduler")
        _ = try scoring.service.stageRating(stageRequest)
        _ = try scoring.service.takeRating(lease: scoreLease, stageID: stageID)
        do { _ = try scoring.service.takeRating(lease: scoreLease, stageID: stageID); preconditionFailure("taken twice") } catch {}
        scoring.failSave = true
        do { _ = try scoring.service.restoreRating(["lease": scoreLease, "stageId": stageID, "revealed": true]); preconditionFailure("failed restore lost its recovery record") } catch {}
        scoring.failSave = false
        let recovered = try scoring.service.restoreRating(["lease": scoreLease, "stageId": stageID, "revealed": true])
        precondition(((recovered["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).count == 1)
        precondition(scoring.service.presentation()["showingAnswer"] as? Bool == true)
        do { _ = try scoring.service.restoreRating(["lease": scoreLease, "stageId": stageID, "revealed": true]); preconditionFailure("same rejected score restored twice") } catch {}

        let localScore = Fixture(); localScore.local = ["hasLocalCards": true, "entries": [], "dueTotal": 0]
        let localLease = try await localScore.service.load(localScore.input())["request"] as! String
        let first: Q.Object = ["entity_id": "same-entity", "entity_index": 0, "_localReview": ["wasDue": true]]
        let sibling: Q.Object = ["entity_id": "same-entity", "entity_index": 1, "_localReview": ["wasDue": false]]
        var localSnapshot = scoreSnapshot
        localSnapshot["cards"] = [first, sibling]; localSnapshot["due_total"] = 0
        _ = try localScore.service.save(localSnapshot, request: localLease)
        _ = try localScore.service.interact(["lease": localLease, "cardId": "same-entity_i0", "key": "reveal"])
        let localID = UUID().uuidString
        let localStage = try localScore.service.stageRating(["lease": localLease, "stageId": localID,
            "snapshot": localSnapshot, "card": first, "cardKey": "same-entity:0", "ease": 1, "revealed": true])
        do { _ = try await localScore.service.load(localScore.input()); preconditionFailure("reload discarded an undoable rating") } catch {}
        do { _ = try localScore.service.improvementInput(lease: localLease, cardID: "same-entity_i1"); preconditionFailure("draft ignored unsaved score") } catch {}
        let localRestored = try localScore.service.undoRating(["lease": localLease, "stageId": localID,
            "snapshot": localStage["snapshot"]!])["snapshot"] as! Q.Object
        precondition((localRestored["cards"] as! [Q.Object]).count == 2 && localRestored["due_total"] as? Int == 0)
        var stale = localSnapshot; stale["index"] = 1
        do {
            _ = try localScore.service.stageRating(["lease": localLease, "stageId": UUID().uuidString,
                "snapshot": stale, "card": sibling, "cardKey": "same-entity:1", "ease": 3, "revealed": true])
            preconditionFailure("caller replaced the authoritative cursor")
        } catch {}
        let projected = try ReaderNativeReviewCards.local(["record": ["id": "card_abcd", "entityRev": 9, "stateRev": 3,
            "source": ["documentId": "localbook:book", "location": ["page": 4]]],
            "card": ["type": "cloze", "cloze": "{{c1::東京::都市}}へ行く", "deck": "Japanese"],
            "cardIndex": 2, "due": true, "state": ["review": ["ease": 3], "projections": ["anki": ["pi-legacy": [
                "status": "succeeded", "cardIds": [7654]]]]]])
        precondition(projected["question"] as? String == "<b>[…]</b>へ行く")
        precondition(projected["answer"] as? String == "<b>東京</b>へ行く")
        precondition(projected["entity_index"] as? Int64 == 2 && projected["_legacyExternalCardId"] as? Int64 == 7654)
        precondition(ReaderNativeReviewCards.stableID(projected) == "card_abcd_i2")
        precondition(ReaderNativeReviewCards.stableID(["id": 17, "entity_id": "same", "entity_index": 2]) == "anki_card_17")
        precondition((projected["_localReview"] as! Q.Object)["contentKeys"] as? [String] == ["cloze", "deck", "type"])
        // Native callers may omit their former duplicate snapshot entirely.
        let stagedWithoutMirror = try localScore.service.stageRating(["lease": localLease, "stageId": localID,
            "card": first, "cardKey": "same-entity:0", "ease": 1, "revealed": true])
        localScore.service.discardRating(lease: localLease, stageID: localID)
        _ = stagedWithoutMirror
        let restoredSelection = try localScore.service.selectCard(["lease": localLease, "current": first, "target": sibling])
        precondition((restoredSelection["snapshot"] as! Q.Object)["index"] as? Int == 1)
        _ = try await localScore.service.load(localScore.input(page: 9))
        do { _ = try localScore.service.stageRating(["lease": localLease, "stageId": localID,
            "snapshot": localSnapshot, "card": first, "cardKey": "same-entity:0", "ease": 1, "revealed": true])
            preconditionFailure("stale page rated") } catch {}
        let sender = Fixture(); sender.reply = ["ok": true, "next": ["interval": -600]]; sender.hold = true
        let payload: Q.Object = ["aid": "rating-1", "card_id": 123, "ease": 3]
        let sending = Task { try await sender.service.answer(payload) }
        while sender.held == nil { await Task.yield() }
        let duplicate = Task { try await sender.service.answer(payload) }
        await Task.yield()
        sender.service.invalidate() // A view leaving must not invent a failed score.
        sender.held?.resume(); sender.held = nil
        let sent = try await sending.value, joined = try await duplicate.value
        precondition(ReaderNativeCardRules.same(sent, joined) && sender.calls.count == 1)
        _ = try await sender.service.answer(payload)
        precondition(sender.calls.count == 1)
        var changed = payload; changed["ease"] = 1
        do { _ = try await sender.service.answer(changed); preconditionFailure("same answer ID changed") } catch {}
        for code in [408, 429, 500, 502, 503, 504, 409] {
            let failure = Fixture(); failure.status = code; failure.reply = ["ok": false, "error": "rejected"]
            let receipt = try await failure.service.answer(payload)
            precondition(receipt["ok"] as? Bool == false)
            precondition(receipt["retryable"] as? Bool == [408, 429, 502, 503, 504].contains(code))
        }
        let offlineAnswer = Fixture(); offlineAnswer.transportError = URLError(.notConnectedToInternet)
        let offlineReceipt = try await offlineAnswer.service.answer(payload)
        precondition(offlineReceipt["retryable"] as? Bool == true)
        let cancelledAnswer = Fixture(); cancelledAnswer.transportError = URLError(.cancelled)
        let cancelledReceipt = try await cancelledAnswer.service.answer(payload)
        precondition(cancelledReceipt["retryable"] as? Bool == false)
        let rejectedQueue = Fixture()
        var rejectedInput = rejectedQueue.input(scope: "all")
        rejectedInput["rejectedCards"] = [["card":["id":7,"question":"未保存评分的原题"], "original_index":0]]
        let restoredQueue = try await rejectedQueue.service.load(rejectedInput)["snapshot"] as! Q.Object
        let restoredCards = restoredQueue["cards"] as! [Q.Object]
        precondition(restoredCards.count == 1 && restoredCards[0]["question"] as? String == "未保存评分的原题")
        precondition(restoredQueue["index"] as? Int == 0)

        let editing = Fixture()
        var canonical: Q.Object = ["id": "card_abcd", "entityRev": 1, "stateRev": 1,
            "cards": [["type": "basic", "front": "原题", "back": "原答案"]],
            "states": ["0": ["phase": "confirmed", "review": ["status": "new"]]], "source": [:]]
        let reopening = Fixture()
        func entry(_ id: String, _ answer: String) -> Q.Object {
            var record = canonical; record["id"] = id
            let card: Q.Object = ["type": "basic", "front": "题目", "back": answer]
            return ["record": record, "card": card, "state": ["phase": "confirmed"], "cardIndex": 0, "due": false]
        }
        reopening.local = ["hasLocalCards": true, "dueTotal": 0, "entries": [entry("card_one", "旧答案"), entry("card_two", "旧答案")]]
        let reopenedInput = reopening.input()
        let reopenedLoad = try await reopening.service.load(reopenedInput)
        let reopenedCards = (reopenedLoad["snapshot"] as! Q.Object)["cards"] as! [Q.Object]
        _ = try reopening.service.selectCard(["lease": reopenedInput["request"]!, "current": reopenedCards[0], "target": reopenedCards[1]])
        reopening.local["entries"] = [entry("card_two", "新答案"), entry("card_one", "旧答案")]
        let resumed = try await reopening.service.load(reopening.input(force: false))["snapshot"] as! Q.Object
        precondition(resumed["index"] as? Int == 0)
        precondition((resumed["cards"] as! [Q.Object])[0]["answer"] as? String == "新答案", "resumed cached card content")
        let sourceCard = try reopening.service.currentCard(context: "ctx-book-4", cardID: "card_two_i0")
        let currentLease = reopening.service.presentation()["lease"] as! String
        let presented = try reopening.service.presentedCard(lease: currentLease, cardID: "card_two_i0")
        precondition(ReaderNativeCardRules.same(sourceCard, presented))
        _ = try reopening.service.interact(["lease": currentLease, "key": "improveMode", "cardId": "card_two_i0", "value": "concise"])
        let improvement = try reopening.service.improvementInput(lease: currentLease, cardID: "card_two_i0")
        precondition(improvement["contextKey"] as? String == "ctx-book-4:current")
        precondition(improvement["verbosity"] as? String == "concise")
        precondition((improvement["card"] as! Q.Object)["answer"] as? String == "新答案")
        precondition((improvement["card"] as! Q.Object)["entity_id"] as? String == "card_two")
        do { _ = try reopening.service.improvementInput(lease: currentLease, cardID: "card_one_i0"); preconditionFailure("old card used for draft") } catch {}
        do { _ = try reopening.service.improvementInput(lease: "stale", cardID: "card_two_i0"); preconditionFailure("old queue used for draft") } catch {}
        do { _ = try reopening.service.presentedCard(lease: "stale-lease", cardID: "card_two_i0"); preconditionFailure("old review lease admitted") } catch {}
        do { _ = try reopening.service.presentedCard(lease: currentLease, cardID: "card_one_i0"); preconditionFailure("old review card admitted") } catch {}
        precondition(sourceCard["entity_id"] as? String == "card_two")
        do { _ = try reopening.service.currentCard(context: "ctx-book-4", cardID: "card_one_i0"); preconditionFailure("old source button admitted") } catch {}
        reopening.failSave = true
        do { _ = try await reopening.service.load(reopening.input(force: false)); preconditionFailure("resume save failure hidden") } catch {}
        editing.local = ["hasLocalCards": true, "dueTotal": 0, "entries": [["record": canonical,
            "card": (canonical["cards"] as! [Q.Object])[0], "state": (canonical["states"] as! Q.Object)["0"]!,
            "cardIndex": 0, "due": false]]]
        let editInput = editing.input(), editLease = editInput["request"] as! String
        let editingLoad = try await editing.service.load(editInput)
        _ = try editing.service.save(editingLoad["snapshot"] as! Q.Object, request: editLease)
        _ = try editing.service.interact(["lease": editLease, "key": "reveal", "cardId": "card_abcd_i0"])
        canonical["stateRev"] = 2
        _ = try editing.service.reconcile(id: "card_abcd", record: canonical, request: editLease)
        precondition(editing.service.presentation()["showingAnswer"] as? Bool == true, "metadata refresh hid the answer")
        let editingSnapshot = try editing.service.peek()!
        let editingCard = (editingSnapshot["cards"] as! [Q.Object])[0]
        let editingStage = UUID().uuidString
        _ = try editing.service.stageRating(["lease": editLease, "stageId": editingStage, "card": editingCard,
            "cardKey": "card_abcd_i0", "ease": 3, "revealed": true])
        canonical["entityRev"] = 2
        canonical["cards"] = [["type": "basic", "front": "改后的题", "back": "原答案"]]
        editing.failSave = true
        do { _ = try editing.service.reconcile(id: "card_abcd", record: canonical, request: editLease); preconditionFailure("failed reconciliation changed the queue") } catch {}
        precondition(editing.service.presentation()["count"] as? Int == 0)
        editing.failSave = false
        let revised = try editing.service.reconcile(id: "card_abcd", record: canonical, request: editLease)
        precondition(revised["discardedStageId"] as? String == editingStage)
        precondition(editing.service.presentation()["showingAnswer"] as? Bool == false)
        precondition((editing.service.presentation()["current"] as! Q.Object)["front"] as? String == "改后的题")
        do { _ = try editing.service.takeRating(lease: editLease, stageID: editingStage); preconditionFailure("changed card kept its former staged score") } catch {}
        canonical["deleted"] = true
        _ = try editing.service.reconcile(id: "card_abcd", record: canonical, request: editLease)
        precondition(editing.service.presentation()["count"] as? Int == 0)
        let deletion = Fixture()
        deletion.reply["cards"] = [["id": 70, "note_id": 7, "question": "one"],
            ["id": 71, "note_id": 7, "question": "sibling"], ["id": 80, "note_id": 8, "question": "other"]]
        let deleteInput = deletion.input(scope: "all"), deleteLease = deleteInput["request"] as! String
        _ = try await deletion.service.load(deleteInput)
        do { _ = try await deletion.service.deleteCurrent(lease: deleteLease, cardID: "anki_card_70", kind: "reader-card", confirmed: true); preconditionFailure("wrong delete range") } catch {}
        do { _ = try await deletion.service.deleteCurrent(lease: deleteLease, cardID: "anki_card_70", kind: "anki-note", confirmed: false); preconditionFailure("unconfirmed delete") } catch {}
        deletion.failReceipt = true
        do { _ = try await deletion.service.deleteCurrent(lease: deleteLease, cardID: "anki_card_70", kind: "anki-note", confirmed: true); preconditionFailure("sent without reservation") } catch {}
        precondition(deletion.calls.count == 1)
        deletion.failReceipt = false; deletion.reply = ["ok": true]
        let removed = try await deletion.service.deleteCurrent(lease: deleteLease, cardID: "anki_card_70", kind: "anki-note", confirmed: true)
        let remaining = (removed["snapshot"] as! Q.Object)["cards"] as! [Q.Object]
        precondition(remaining.count == 1 && remaining[0]["id"] as? Int == 80)
        precondition(deletion.calls.count == 2)
        let uncertain = Fixture(); uncertain.reply["cards"] = [["id": 90, "note_id": 9, "question": "unknown"]]
        let uncertainInput = uncertain.input(scope: "all"), uncertainLease = uncertainInput["request"] as! String
        _ = try await uncertain.service.load(uncertainInput)
        uncertain.reply = [:]
        for _ in 0..<2 {
            do { _ = try await uncertain.service.deleteCurrent(lease: uncertainLease, cardID: "anki_card_90", kind: "anki-note", confirmed: true); preconditionFailure("unknown delete succeeded") } catch {}
        }
        precondition(uncertain.calls.count == 2 && uncertain.service.presentation()["count"] as? Int == 1)
        let restarted = Fixture(); restarted.receipts = uncertain.receipts
        restarted.reply["cards"] = [["id": 90, "note_id": 9, "question": "unknown"]]
        let restartInput = restarted.input(scope: "all")
        _ = try await restarted.service.load(restartInput)
        do { _ = try await restarted.service.deleteCurrent(lease: restartInput["request"] as! String, cardID: "anki_card_90", kind: "anki-note", confirmed: true); preconditionFailure("restart resubmitted unknown delete") } catch {}
        precondition(restarted.calls.count == 1)
        let localDelete = Fixture()
        let group: Q.Object = ["id": "card_group", "entityRev": 1, "stateRev": 2,
            "cards": [["type": "basic", "front": "one", "back": "a"], ["type": "basic", "front": "two", "back": "b"]],
            "states": ["0": ["phase": "confirmed"], "1": ["phase": "confirmed"]]]
        localDelete.local = ["hasLocalCards": true, "dueTotal": 0, "entries": (0..<2).map { index -> Q.Object in
            ["record": group, "card": (group["cards"] as! [Q.Object])[index],
             "state": ["phase": "confirmed"], "cardIndex": index, "due": false]
        }]
        localDelete.removedRecord = group
        localDelete.removedRecord["stateRev"] = 3
        localDelete.removedRecord["states"] = ["0": ["phase": "confirmed", "removed": true], "1": ["phase": "confirmed"]]
        let localDeleteInput = localDelete.input()
        _ = try await localDelete.service.load(localDeleteInput)
        let localRemoved = try await localDelete.service.deleteCurrent(lease: localDeleteInput["request"] as! String,
            cardID: "card_group_i0", kind: "reader-card", confirmed: true)
        let sibling = ((localRemoved["snapshot"] as! Q.Object)["cards"] as! [Q.Object]).first!
        precondition(ReaderNativeReviewCards.stableID(sibling) == "card_group_i1")
        precondition((sibling["_localReview"] as! Q.Object)["stateRev"] as? Int == 3, "sibling kept an obsolete group revision")
        precondition(localDelete.calls.isEmpty && localDelete.removedInputs.count == 1)
        let lateDelete = Fixture(); lateDelete.reply["cards"] = [["id": 100, "note_id": 10, "question": "late"]]
        let lateInput = lateDelete.input(scope: "all"), lateLease = lateInput["request"] as! String
        _ = try await lateDelete.service.load(lateInput)
        lateDelete.hold = true; lateDelete.reply = ["ok": true]
        let deleting = Task { try await lateDelete.service.deleteCurrent(lease: lateLease, cardID: "anki_card_100", kind: "anki-note", confirmed: true) }
        while lateDelete.held == nil { await Task.yield() }
        do { _ = try await lateDelete.service.load(lateDelete.input()); preconditionFailure("load discarded in-flight deletion") } catch {}
        lateDelete.service.cancel(lateLease)
        lateDelete.held?.resume(); lateDelete.held = nil
        let lateResult = try await deleting.value
        precondition(lateResult["deleted"] as? Bool == true && lateResult["snapshot"] == nil)
        precondition(lateDelete.receipts.values.contains { $0.contains("succeeded") }, "late success receipt was lost")
        func scoreStage(_ value: Fixture, ease: Int = 3) async throws -> (String, String) {
            let input = value.input(scope: "all"), lease = input["request"] as! String, id = UUID().uuidString
            _ = try await value.service.load(input)
            _ = try value.service.revealCurrent(lease: lease, cardID: "anki_card_7")
            _ = try value.service.stageCurrentRating(lease: lease, cardID: "anki_card_7", stageID: id, ease: ease)
            return (lease, id)
        }
        let effects = Fixture(); effects.holdScore = true
        let (effectLease, effectID) = try await scoreStage(effects, ease: 1)
        let firstScore = Task { try await effects.service.commitRating(lease: effectLease, stageID: effectID) }
        while effects.heldScore == nil { await Task.yield() }
        let joinedScore = Task { try await effects.service.commitRating(lease: effectLease, stageID: effectID) }
        await Task.yield()
        do { _ = try await effects.service.load(effects.input()); preconditionFailure("load dropped in-flight score") } catch {}
        effects.heldScore?.resume(); effects.heldScore = nil
        let effectResult = try await firstScore.value, joinedResult = try await joinedScore.value
        precondition(effectResult["ok"] as? Bool == true && joinedResult["ok"] as? Bool == true && effects.scoreCalls == 1)
        precondition(effects.service.presentation()["count"] as? Int == 1, "again must requeue once after acceptance")
        _ = try await effects.service.commitRating(lease: effectLease, stageID: effectID)
        precondition(effects.scoreCalls == 1)
        let rejection = Fixture(); rejection.scoreResult = ["ok": false, "error": "stale scheduler"]
        let (rejectLease, rejectID) = try await scoreStage(rejection)
        let rejected = try await rejection.service.commitRating(lease: rejectLease, stageID: rejectID)
        precondition(rejected["ok"] as? Bool == false && rejection.service.presentation()["count"] as? Int == 1)
        let afterScore = Fixture(); afterScore.failAfterScore = true
        let (afterLease, afterID) = try await scoreStage(afterScore, ease: 1)
        let incomplete = try await afterScore.service.commitRating(lease: afterLease, stageID: afterID)
        precondition(incomplete["ok"] as? Bool == true && incomplete["projectionPending"] as? Bool == true)
        afterScore.failSave = false
        let repaired = try await afterScore.service.commitRating(lease: afterLease, stageID: afterID)
        precondition(repaired["projectionPending"] == nil && afterScore.scoreCalls == 1)
        precondition(afterScore.service.presentation()["count"] as? Int == 1)
        let beforeScore = Fixture()
        let (beforeLease, beforeID) = try await scoreStage(beforeScore)
        beforeScore.failSave = true
        let notSent = try await beforeScore.service.commitRating(lease: beforeLease, stageID: beforeID)
        precondition(notSent["ok"] as? Bool == false && beforeScore.scoreCalls == 0)
        beforeScore.failSave = false
        _ = try await beforeScore.service.commitRating(lease: beforeLease, stageID: beforeID)
        precondition(beforeScore.service.presentation()["count"] as? Int == 1 && beforeScore.scoreCalls == 0)
        print("Native review acquisition, reversible staging, deletion and failure recovery passed")
    }
}
