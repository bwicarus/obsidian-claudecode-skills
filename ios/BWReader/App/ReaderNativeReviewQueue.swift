import Foundation

/// Queue acquisition and its one device-local recovery snapshot. Reader cards
/// remain canonical in CardRepository; remote cards retain server identities.
/// No view nodes, schedulers or new card records are created by this owner.
@MainActor
final class ReaderNativeReviewQueue {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    struct Response { let status: Int; let data: Data }
    typealias Object = [String: Any]
    typealias Fetch = (String, String, Data) async throws -> Response
    private let local: () throws -> Object
    private let read: () throws -> String?
    private let write: (String) throws -> Void
    private let fetch: Fetch
    private let now: () -> Double
    private var lease = ""
    private var contextKey = ""
    private var identity = Data()
    private var scope = "current"
    private var task: Task<Response, Error>?
    private var cancelled = false
    private var stagedRating: Object?
    private var activeSnapshot: Object?
    private var stagedSource: Object?
    private var showingAnswer = false
    private var expanded = true
    private var improveMode = "verbose"
    private var takenRatings: [String: Object] = [:]
    private var presentationRevision = 0
    private var pendingAnswers: [String: (Data, Task<Object, Never>)] = [:]
    private var answerReceipts: [String: (Data, Object)] = [:]
    private var answerOrder: [String] = []
    static let cacheKey = "native-review-queue-v1"

    init(local: @escaping () throws -> Object, read: @escaping () throws -> String?,
         write: @escaping (String) throws -> Void, fetch: @escaping Fetch,
         now: @escaping () -> Double = { Date().timeIntervalSince1970 * 1000 }) {
        self.local = local; self.read = read; self.write = write; self.fetch = fetch; self.now = now
    }
    func invalidate() {
        task?.cancel(); task = nil; lease = ""; contextKey = ""; identity = Data(); stagedRating = nil
        activeSnapshot = nil; stagedSource = nil
        showingAnswer = false; expanded = true
        takenRatings = [:]
    }
    func cancel(_ id: String) {
        if lease == id { cancelled = true; task?.cancel(); task = nil }
    }
    private func current(_ id: String) throws {
        try Task.checkCancellation()
        guard lease == id, !cancelled else { throw CancellationError() }
    }
    private static func bytes(_ value: Object) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value) else { throw Failure(message: "复习数据不是有效 JSON") }
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed])
        guard data.count <= 8 * 1024 * 1024 else { throw Failure(message: "复习批次过大") }
        return data
    }
    private static func count(_ value: Any?, maximum: Int = 1_000_000_000) throws -> Int {
        let number = try ReaderNativeCardRules.integer(value ?? 0, "review count")
        guard number >= 0, number <= maximum else { throw Failure(message: "复习数量无效") }
        return Int(number)
    }
    static func snapshot(_ raw: Object) throws -> Object {
        guard let key = raw["client_context_key"] as? String, !key.isEmpty, key.utf16.count <= 240,
              let cards = raw["cards"] as? [Object], cards.count <= 200,
              let completed = raw["completed_ids"] as? [Any], completed.count <= 100 else {
            throw Failure(message: "复习快照不完整，未覆盖旧数据")
        }
        let ids: [Any] = try completed.map { item in
            if let string = item as? String, !string.isEmpty, string.utf16.count <= 240 { return string }
            let number = try ReaderNativeCardRules.integer(item, "completed card id")
            guard number > 0 else { throw Failure(message: "已完成卡号无效") }; return number
        }
        let stamp = try ReaderNativeCardRules.number(raw["ts"], "review timestamp")
        guard stamp >= 0 else { throw Failure(message: "复习时间无效") }
        let index = try count(raw["index"], maximum: 200)
        let result: Object = ["ts": stamp, "client_context_key": key, "cards": cards,
            "index": min(index, max(0, cards.count - 1)), "completed_ids": ids,
            "due_total": try count(raw["due_total"]), "related_total": try count(raw["related_total"])]
        _ = try bytes(result); return result
    }
    private func cached() throws -> Object? {
        guard let text = try read(), text.utf8.count <= 8 * 1024 * 1024,
              let record = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? Object,
              record["contract"] as? String == "native-review-queue/1",
              let savedIdentity = record["identity"] as? String, savedIdentity == identity.base64EncodedString(),
              let value = record["snapshot"] as? Object else { return nil }
        return try Self.snapshot(value)
    }
    func peek() throws -> Object? { try cached() }

    /// Buttons refer to the displayed card, never to a card body supplied by a
    /// view or an old webpage. This also protects navigation after a queue refresh.
    func currentCard(context: String, cardID: String) throws -> Object {
        guard !cancelled, !lease.isEmpty, context == contextKey,
              let cards = activeSnapshot?["cards"] as? [Object],
              let index = activeSnapshot?["index"] as? Int, cards.indices.contains(index),
              ReaderNativeReviewCards.stableID(cards[index]) == cardID else {
            throw Failure(message: "当前复习卡已变化，请重新选择")
        }
        return cards[index]
    }

    func presentation() -> Object {
        presentationRevision += 1
        let cards = activeSnapshot?["cards"] as? [Object] ?? []
        let index = activeSnapshot?["index"] as? Int ?? 0
        func card(_ offset: Int) -> Any {
            cards.indices.contains(index + offset) ? ReaderNativeReviewCards.assistant(cards[index + offset]) : NSNull()
        }
        return ["lease": lease, "revision": presentationRevision, "contextKey": contextKey, "scope": scope,
            "index": index, "count": cards.count, "queueIds": cards.map(ReaderNativeReviewCards.stableID),
            "dueTotal": activeSnapshot?["due_total"] ?? 0, "relatedTotal": activeSnapshot?["related_total"] ?? 0,
            "current": card(0), "previous": card(-1), "next": card(1),
            "deleteKind": ReaderNativeReviewCards.deleteKind(cards.indices.contains(index) ? cards[index] : nil),
            "showingAnswer": showingAnswer, "expanded": expanded, "improveMode": improveMode]
    }

    /// Presentation actions are validated against the same card and queue lease
    /// as rating. Neither a web callback nor an old button can reveal a new card.
    func interact(_ input: Object) throws -> Object {
        guard input["lease"] as? String == lease, !lease.isEmpty, !cancelled,
              let key = input["key"] as? String else { throw Failure(message: "复习界面已切换") }
        let source = try commandSnapshot(input)
        let cards = source["cards"] as! [Object], index = source["index"] as! Int
        let currentID = cards.indices.contains(index) ? ReaderNativeReviewCards.stableID(cards[index]) : ""
        guard input["cardId"] as? String == currentID else { throw Failure(message: "当前复习卡已变化") }
        switch key {
        case "reveal":
            guard !currentID.isEmpty, stagedRating == nil else { throw Failure(message: "请先保存上一张卡的评分") }
            showingAnswer = true
        case "expanded": expanded = try ReaderNativeCardRules.bool(input["enabled"], "expanded")
        case "improveMode":
            guard let value = input["value"] as? String, ["concise", "verbose"].contains(value) else { throw Failure(message: "草稿模式无效") }
            improveMode = value
        default: throw Failure(message: "未知复习界面操作")
        }
        return presentation()
    }

    /// Same endpoint and answer ID as the existing scheduler adapter. Joining
    /// an in-flight answer does not issue another write. Durable offline retry
    /// remains in the account-scoped command outbox until its own migration.
    func answer(_ input: Object) async throws -> Object {
        guard let aid = input["aid"] as? String, !aid.isEmpty, aid.utf16.count <= 240,
              !aid.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            throw Failure(message: "评分缺少操作编号")
        }
        let card = try ReaderNativeCardRules.integer(input["card_id"], "Anki card id")
        let ease = try ReaderNativeCardRules.integer(input["ease"], "ease")
        guard card > 0, (1...4).contains(ease) else { throw Failure(message: "评分参数无效") }
        let body = try Self.bytes(["aid": aid, "card_id": card, "ease": ease])
        if let saved = answerReceipts[aid] {
            guard saved.0 == body else { throw Failure(message: "同一评分编号的内容发生变化") }
            return saved.1
        }
        if let pending = pendingAnswers[aid] {
            guard pending.0 == body else { throw Failure(message: "同一评分编号的内容发生变化") }
            return await pending.1.value
        }
        guard pendingAnswers.count < 32 else { throw Failure(message: "仍有评分等待回执，请稍后重试") }
        let work = Task<Object, Never> { [fetch] in
            do {
                let response = try await fetch("/pdf/api/review-answer", "POST", body)
                guard response.data.count <= 8 * 1024 * 1024 else {
                    return ["ok": false, "retryable": false, "status": response.status, "error": "评分回执过大，结果待确认"]
                }
                let value = (try? JSONSerialization.jsonObject(with: response.data)) as? Object ?? [:]
                if (200..<300).contains(response.status), value["ok"] as? Bool != false {
                    return ["ok": true, "value": value]
                }
                return ["ok": false, "status": response.status,
                        "retryable": [408, 429, 502, 503, 504].contains(response.status),
                        "error": value["error"] as? String ?? "HTTP \(response.status)"]
            } catch {
                let network = error as NSError
                // A cancelled/context-invalid request is not a transport failure
                // and must not be silently re-enqueued under a later book.
                let retry = network.domain == NSURLErrorDomain && network.code != NSURLErrorCancelled
                return ["ok": false, "status": 0, "retryable": retry, "error": error.localizedDescription]
            }
        }
        pendingAnswers[aid] = (body, work)
        let receipt = await work.value
        pendingAnswers.removeValue(forKey: aid)
        answerReceipts[aid] = (body, receipt); answerOrder.append(aid)
        while answerOrder.count > 128 { answerReceipts.removeValue(forKey: answerOrder.removeFirst()) }
        return receipt
    }

    private static func sameCard(_ left: Object, _ right: Object) -> Bool {
        for field in ["id", "note_id"] {
            let first = ReaderNativeCardRules.string(left[field]), second = ReaderNativeCardRules.string(right[field])
            if !first.isEmpty || !second.isEmpty { return first.utf16.elementsEqual(second.utf16) }
        }
        let entity = ReaderNativeCardRules.string(left["entity_id"])
        if !entity.isEmpty || !ReaderNativeCardRules.string(right["entity_id"]).isEmpty {
            return entity.utf16.elementsEqual(ReaderNativeCardRules.string(right["entity_id"]).utf16) &&
                ReaderNativeCardRules.string(left["entity_index"]) == ReaderNativeCardRules.string(right["entity_index"])
        }
        let local = ReaderNativeCardRules.string(left["local_id"])
        if !local.isEmpty { return local.utf16.elementsEqual(ReaderNativeCardRules.string(right["local_id"]).utf16) }
        return ReaderNativeCardRules.same(left, right)
    }

    /// UI commands reference the committed queue, rather than bringing another
    /// mutable copy of it. An optional compatibility snapshot is compare-only.
    private func commandSnapshot(_ input: Object) throws -> Object {
        guard let activeSnapshot else { throw Failure(message: "复习队列尚未就绪") }
        if let raw = input["snapshot"] as? Object {
            let incoming = try Self.snapshot(raw)
            let fields = ["client_context_key", "cards", "index", "completed_ids", "due_total", "related_total"]
            guard fields.allSatisfy({ ReaderNativeCardRules.same(incoming[$0]!, activeSnapshot[$0]!) }) else {
                throw Failure(message: "复习队列已更新，请使用当前卡片")
            }
        }
        return activeSnapshot
    }

    /// Change only the active card, preserving queue order, original identities
    /// and completed IDs. Persistence must succeed before the UI advances.
    func selectCard(_ input: Object) throws -> Object {
        guard input["lease"] as? String == lease, !lease.isEmpty, !cancelled,
              stagedRating == nil,
              let currentCard = input["current"] as? Object,
              let targetCard = input["target"] as? Object else {
            throw Failure(message: "复习卡片切换已失效")
        }
        var source = try commandSnapshot(input)
        guard source["client_context_key"] as? String == contextKey else { throw Failure(message: "复习内容已切换") }
        let cards = source["cards"] as! [Object], index = source["index"] as! Int
        guard cards.indices.contains(index), ReaderNativeCardRules.same(cards[index], currentCard) else {
            throw Failure(message: "当前复习卡已更新")
        }
        let matches = cards.indices.filter { Self.sameCard(cards[$0], targetCard) }
        guard matches.count == 1, let target = matches.first,
              ReaderNativeCardRules.same(cards[target], targetCard) else { throw Failure(message: "目标复习卡已更新或不唯一") }
        source["index"] = target; source["native_queue_lease"] = lease
        if target != index { try save(source, request: lease); showingAnswer = false }
        return ["changed": target != index, "snapshot": source]
    }

    /// One action of undo is a reversible in-memory stage, not an external
    /// scheduler undo. The persisted recovery snapshot keeps the original card
    /// until the existing commit operation has a durable result.
    func stageRating(_ input: Object) throws -> Object {
        guard input["lease"] as? String == lease, !lease.isEmpty, !cancelled,
              let id = input["stageId"] as? String, UUID(uuidString: id) != nil,
              let card = input["card"] as? Object,
              let cardKey = input["cardKey"] as? String, !cardKey.isEmpty, cardKey.utf16.count <= 1024,
              input["revealed"] as? Bool == true, showingAnswer else { throw Failure(message: "请在当前卡片显示答案后评分") }
        guard stagedRating == nil, takenRatings.isEmpty else { throw Failure(message: "上一张卡的评分尚未处理") }
        var source = try commandSnapshot(input)
        let before = source
        guard source["client_context_key"] as? String == contextKey else { throw Failure(message: "复习内容已切换") }
        var cards = source["cards"] as! [Object]
        let index = source["index"] as! Int
        guard cards.indices.contains(index), ReaderNativeCardRules.same(cards[index], card) else { throw Failure(message: "当前复习卡已更新") }
        let ease = try ReaderNativeCardRules.integer(input["ease"], "ease")
        guard (1...4).contains(ease) else { throw Failure(message: "评分必须为 1 到 4") }
        var completed = source["completed_ids"] as! [Any]
        let completedBefore = completed
        let local = card["_localReview"] as? Object
        let cardID = ReaderNativeCardRules.string(card["id"])
        let completedAdded = local == nil && ease != 1 && !completed.contains { ReaderNativeCardRules.string($0) == cardID }
        if completedAdded {
            guard !cardID.isEmpty else { throw Failure(message: "评分缺少外部卡片编号") }
            completed.append(card["id"]!); completed = Array(completed.suffix(100))
        }
        let dueDecremented = local?["wasDue"] as? Bool == true && (source["due_total"] as! Int) > 0
        cards.remove(at: index)
        source["cards"] = cards; source["completed_ids"] = completed
        source["index"] = min(index, max(0, cards.count - 1))
        if dueDecremented { source["due_total"] = max(0, (source["due_total"] as! Int) - 1) }
        source["native_queue_lease"] = lease
        let stage: Object = ["nativeStageID": id, "nativeQueueLease": lease, "card": card, "ease": ease,
            "pendingKey": contextKey + ":" + cardKey + ":" + String(index), "originalIndex": index,
            "contextKey": contextKey, "dueDecremented": dueDecremented, "completedAdded": completedAdded,
            "completedBefore": completedBefore, "snapshot": source]
        stagedRating = stage
        stagedSource = before; activeSnapshot = try Self.snapshot(source)
        showingAnswer = false
        return ["stage": stage, "snapshot": source]
    }

    /// Take is single-use even if two UI/voice commands arrive together.
    func takeRating(lease expectedLease: String, stageID: String) throws -> Object {
        guard expectedLease == lease, let stage = stagedRating,
              stage["nativeStageID"] as? String == stageID else { throw Failure(message: "暂存评分已处理或已切换") }
        takenRatings[stageID] = stage
        stagedRating = nil; stagedSource = nil; return stage
    }

    func completeRating(_ input: Object) {
        guard input["lease"] as? String == lease, let id = input["stageId"] as? String else { return }
        takenRatings.removeValue(forKey: id)
    }

    func restoreRating(_ input: Object) throws -> Object {
        guard input["lease"] as? String == lease, !cancelled,
              let id = input["stageId"] as? String,
              let stage = takenRatings[id] ?? (stagedRating?["nativeStageID"] as? String == id ? stagedRating : nil),
              let activeSnapshot else { throw Failure(message: "评分恢复轮次已失效") }
        let restored = try restoredSnapshot(stage, from: activeSnapshot)
        guard try save(restored, request: lease) else { throw Failure(message: "评分恢复上下文已变化") }
        takenRatings.removeValue(forKey: id)
        if stagedRating?["nativeStageID"] as? String == id { stagedRating = nil; stagedSource = nil }
        showingAnswer = input["revealed"] as? Bool == true
        return ["snapshot": restored]
    }

    func discardRating(lease expectedLease: String, stageID: String) {
        guard expectedLease == lease, stagedRating?["nativeStageID"] as? String == stageID else { return }
        if let stagedSource, let stage = stagedRating, let after = stage["snapshot"] as? Object,
           let normalized = try? Self.snapshot(after), let activeSnapshot,
           ReaderNativeCardRules.same(activeSnapshot, normalized) {
            self.activeSnapshot = stagedSource
            showingAnswer = true
        }
        stagedRating = nil; stagedSource = nil
    }

    func undoRating(_ input: Object) throws -> Object {
        guard input["lease"] as? String == lease, !cancelled, let stage = stagedRating,
              (stage["nativeStageID"] as? String) == (input["stageId"] as? String) else { throw Failure(message: "当前没有可撤回的暂存评分") }
        var source = try commandSnapshot(input)
        guard source["client_context_key"] as? String == contextKey else { throw Failure(message: "复习内容已切换") }
        source = try restoredSnapshot(stage, from: source)
        // A failed save retains the stage so the user can retry undo; no score
        // is sent to a scheduler by this path.
        try save(source, request: lease)
        stagedRating = nil; stagedSource = nil
        showingAnswer = true
        return ["stage": stage, "snapshot": source]
    }

    private func restoredSnapshot(_ stage: Object, from original: Object) throws -> Object {
        var source = original
        let card = stage["card"] as! Object
        var cards = (source["cards"] as! [Object]).filter { !Self.sameCard($0, card) }
        let index = min(stage["originalIndex"] as! Int, cards.count)
        cards.insert(card, at: index)
        source["cards"] = cards; source["index"] = index; source["native_queue_lease"] = lease
        if stage["dueDecremented"] as? Bool == true { source["due_total"] = (source["due_total"] as! Int) + 1 }
        if stage["completedAdded"] as? Bool == true {
            var restored = (source["completed_ids"] as! [Any]).filter {
                ReaderNativeCardRules.string($0) != ReaderNativeCardRules.string(card["id"])
            }
            // Staging at the 100-item boundary evicts an older ID. Undo restores
            // that ID as well, while retaining later compatible observations.
            let retained = Set(restored.map { ReaderNativeCardRules.string($0) })
            restored = (stage["completedBefore"] as! [Any]).filter {
                !retained.contains(ReaderNativeCardRules.string($0))
            } + restored
            source["completed_ids"] = Array(restored.suffix(100))
        }
        return source
    }

    /// Repository notifications carry identity only. The caller reads the
    /// canonical record in Swift; web copies cannot replace queue contents.
    func reconcile(id: String, record: Object?, request: String) throws -> Object {
        guard request == lease, !cancelled, let original = activeSnapshot else { return ["updated": false] }
        if let record, record["id"] as? String != id { throw Failure(message: "复习卡组身份不匹配") }
        let oldCards = original["cards"] as! [Object], oldIndex = original["index"] as! Int
        let oldCurrent = oldCards.indices.contains(oldIndex) ? oldCards[oldIndex] : nil
        func changed(_ snapshot: Object) throws -> Object {
            var result = snapshot, cards = snapshot["cards"] as! [Object], index = snapshot["index"] as! Int
            var due = snapshot["due_total"] as! Int
            for offset in cards.indices.reversed() {
                let before = cards[offset]
                guard let local = before["_localReview"] as? Object, local["gid"] as? String == id,
                      let cardIndex = (local["cardIndex"] as? NSNumber)?.intValue else { continue }
                let originals = record?["cards"] as? [Object] ?? []
                let card = originals.indices.contains(cardIndex) ? originals[cardIndex] : nil
                let state = (record?["states"] as? Object)?[String(cardIndex)] as? Object
                let review = state?["review"] as? Object ?? [:]
                let status = (review["status"] as? String ?? "new").lowercased()
                let dueAt = (review["dueAt"] as? NSNumber)?.doubleValue
                let unavailable = ["unavailable", "suspended", "buried"].contains(status) ||
                    (status != "new" && dueAt.map { $0 > now() } == true)
                if card == nil || state == nil || record?["deleted"] as? Bool == true ||
                    state?["removed"] as? Bool == true || state?["phase"] as? String != "confirmed" ||
                    (state?["flags"] as? Object)?["archived"] as? Bool == true || unavailable {
                    cards.remove(at: offset)
                    if offset < index { index -= 1 }
                    if local["wasDue"] as? Bool == true { due = max(0, due - 1) }
                } else {
                    cards[offset] = try ReaderNativeReviewCards.local(["record": record!, "card": card!,
                        "state": state!, "cardIndex": cardIndex, "due": local["wasDue"] as? Bool == true])
                }
            }
            result["cards"] = cards; result["index"] = min(index, max(0, cards.count - 1)); result["due_total"] = due
            return try Self.snapshot(result)
        }
        let durable = try changed(stagedSource ?? original)
        var projected = try changed(original)
        var discarded = ""
        if let stage = stagedRating, let card = stage["card"] as? Object,
           (card["_localReview"] as? Object)?["gid"] as? String == id {
            let matching = (durable["cards"] as! [Object]).first { Self.sameCard($0, card) }
            if matching == nil || !ReaderNativeCardRules.same(matching!, card) {
                // A changed card cannot keep a score staged against its former
                // content/revision. No scheduler has received this stage.
                discarded = stage["nativeStageID"] as? String ?? ""
                projected = durable
                let cursor = (durable["cards"] as! [Object]).firstIndex { Self.sameCard($0, card) }
                if let cursor { projected["index"] = cursor }
            }
        }
        guard !ReaderNativeCardRules.same(projected, original) || !discarded.isEmpty else { return ["updated": false] }
        let wasRevealed = showingAnswer
        guard try save(discarded.isEmpty ? durable : projected, request: request) else { return ["updated": false] }
        activeSnapshot = projected
        if !discarded.isEmpty { stagedRating = nil; stagedSource = nil }
        else if var stage = stagedRating {
            stagedSource = durable; stage["snapshot"] = projected
            if let card = stage["card"] as? Object,
               let index = (durable["cards"] as! [Object]).firstIndex(where: { Self.sameCard($0, card) }) { stage["originalIndex"] = index }
            stagedRating = stage
        }
        let cards = projected["cards"] as! [Object], index = projected["index"] as! Int
        let current = cards.indices.contains(index) ? cards[index] : nil
        let changedCurrent = oldCurrent == nil || current == nil ||
            ReaderNativeReviewCards.stableID(oldCurrent!) != ReaderNativeReviewCards.stableID(current!) ||
            !ReaderNativeCardRules.same(ReaderNativeReviewCards.assistant(oldCurrent!), ReaderNativeReviewCards.assistant(current!))
        showingAnswer = wasRevealed && !changedCurrent && discarded.isEmpty
        return ["updated": true, "snapshot": projected, "changedCurrent": changedCurrent,
                "discardedStageId": discarded]
    }
    @discardableResult
    func save(_ value: Object, request: String) throws -> Bool {
        // Late save chains from another book, scope or load are observations,
        // not a reason to overwrite the active recovery snapshot.
        guard !lease.isEmpty, request == lease, !cancelled,
              value["client_context_key"] as? String == contextKey else { return false }
        let snapshot = try Self.snapshot(value)
        let record: Object = ["contract": "native-review-queue/1", "identity": identity.base64EncodedString(), "snapshot": snapshot]
        try write(String(decoding: Self.bytes(record), as: UTF8.self))
        let oldCards = activeSnapshot?["cards"] as? [Object] ?? []
        let oldIndex = activeSnapshot?["index"] as? Int ?? 0
        let cards = snapshot["cards"] as! [Object], index = snapshot["index"] as! Int
        if !oldCards.indices.contains(oldIndex) || !cards.indices.contains(index) ||
            ReaderNativeReviewCards.stableID(oldCards[oldIndex]) != ReaderNativeReviewCards.stableID(cards[index]) ||
            !["question", "answer", "front", "back"].allSatisfy({
                ReaderNativeCardRules.string(oldCards[oldIndex][$0]) == ReaderNativeCardRules.string(cards[index][$0])
            }) { showingAnswer = false }
        activeSnapshot = snapshot
        return true
    }
    private func request(_ path: String, method: String = "GET", body: Object? = nil, id: String) async throws -> Object {
        try current(id)
        let data = try body.map(Self.bytes) ?? Data()
        let work = Task { [fetch] in try await fetch(path, method, data) }; task = work
        defer { if lease == id { task = nil } }
        let response = try await work.value
        try current(id)
        guard response.data.count <= 8 * 1024 * 1024,
              let result = try JSONSerialization.jsonObject(with: response.data) as? Object,
              (200..<300).contains(response.status), result["ok"] as? Bool == true,
              let cards = result["cards"] as? [Object], cards.count <= 200 else {
            throw Failure(message: "复习服务返回失败或不完整数据")
        }
        return result
    }
    func load(_ input: Object) async throws -> Object {
        guard let id = input["request"] as? String, UUID(uuidString: id) != nil,
              let key = input["contextKey"] as? String, !key.isEmpty, key.utf16.count <= 240,
              let requestedScope = input["scope"] as? String, ["all", "current"].contains(requestedScope),
              let context = input["context"] as? Object, let force = input["force"] as? Bool else {
            throw Failure(message: "复习请求缺少上下文或轮次")
        }
        guard try Self.bytes(context).count <= 32 * 1024 else { throw Failure(message: "复习上下文过大") }
        task?.cancel(); task = nil; cancelled = false; lease = id; contextKey = key; scope = requestedScope; stagedRating = nil
        stagedSource = nil; activeSnapshot = nil
        showingAnswer = false
        takenRatings = [:]
        identity = try Self.bytes(["scope": scope, "context": context])
        func preparedLocal() throws -> Object? {
            let result = try local()
            guard let hasLocal = result["hasLocalCards"] as? Bool,
                  let entries = result["entries"] as? [Object], entries.count <= 30 else {
                throw Failure(message: "本机卡库返回无效数据")
            }
            let due = try Self.count(result["dueTotal"])
            guard hasLocal else { return nil }
            let cards = try entries.map(ReaderNativeReviewCards.local)
            var index = 0
            // Reopening a panel must not jump to its first card. Reuse only the
            // cursor identity; every face/state still comes from today's store.
            if !force, let previous = try cached(),
               previous["client_context_key"] as? String == key,
               let previousCards = previous["cards"] as? [Object], let previousIndex = previous["index"] as? Int,
               previousCards.indices.contains(previousIndex), let stamp = previous["ts"] as? Double,
               now() >= stamp, now() - stamp < 30 * 60 * 1000 {
                let selected = ReaderNativeReviewCards.stableID(previousCards[previousIndex])
                index = cards.firstIndex(where: { ReaderNativeReviewCards.stableID($0) == selected }) ?? 0
            }
            let snapshot = try Self.snapshot(["ts": now(), "client_context_key": key,
                "cards": cards, "index": index,
                "due_total": due, "related_total": 0, "completed_ids": []])
            try save(snapshot, request: id)
            return ["kind": "local", "snapshot": snapshot, "request": id]
        }
        // A corrupt/unavailable local repository must never become a remote
        // fallback, including the valid case of an empty local due queue.
        if let result = try preparedLocal() { return result }
        var cache = try cached()
        if cache == nil, scope == "current", let old = input["legacyCache"] as? Object,
           old["client_context_key"] as? String == key {
            cache = try Self.snapshot(old)
        }
        let age = cache.map { now() - (($0["ts"] as? NSNumber)?.doubleValue ?? 0) } ?? .infinity
        let rejected = Set((input["rejectedIds"] as? [String] ?? []).prefix(200))
        let completed: [Any] = age >= 0 && age < 12 * 60 * 60 * 1000
            ? cache?["completed_ids"] as? [Any] ?? [] : []
        let filtered = completed.filter { !rejected.contains(String(describing: $0)) }
        func finish(_ data: Object, kind: String, notice: String = "", trimRelated: Bool = false) throws -> Object {
            try current(id)
            // Local cards might have arrived while a remote request awaited.
            if let result = try preparedLocal() { return result }
            var cards = data["cards"] as? [Object] ?? []
            let related = try Self.count(data["related_total"])
            if trimRelated, data["related_total"] != nil, !(data["related_total"] is NSNull) { cards = Array(cards.prefix(related)) }
            let snapshot: Object = ["ts": now(), "client_context_key": key, "cards": cards,
                "index": data["index"] ?? 0, "due_total": try Self.count(data["due_total"]),
                "related_total": related, "completed_ids": filtered]
            try save(snapshot, request: id)
            return ["kind": kind, "snapshot": snapshot, "request": id, "notice": notice]
        }
        if !force, age >= 0, age < 30 * 60 * 1000, let cache, !(cache["cards"] as? [Any] ?? []).isEmpty {
            return try finish(cache, kind: "cache")
        }
        let hasContext = scope == "current" && ["file", "url", "source_ref", "selection", "visible_text"].contains {
            !(context[$0] as? String ?? "").isEmpty
        }
        var result: Object
        var kind = "load", notice = "", trim = hasContext
        do {
            result = try await request(hasContext ? "/pdf/api/review-queue" : "/pdf/api/review-queue?limit=30",
                method: hasContext ? "POST" : "GET", body: hasContext ? ["limit": 30, "context": context, "exclude_card_ids": filtered] : nil, id: id)
        } catch {
            try current(id)
            var fallback: Object?
            if hasContext {
                do {
                    fallback = try await request("/pdf/api/review-queue?limit=30", id: id)
                    fallback?["related_total"] = 0
                } catch { try current(id) }
            }
            trim = false
            if let fallback { result = fallback; kind = "fallback"; notice = "相关卡暂不可用，已退回到期卡" }
            else if let cache, !(cache["cards"] as? [Any] ?? []).isEmpty {
                result = cache; kind = "offline"; notice = "离线：使用当前内容的本机复习快照"
            } else { throw error }
        }
        // Database failures are not transport failures. Do not restart a GET
        // or revive an older snapshot if this final canonical check/save fails.
        return try finish(result, kind: kind, notice: notice, trimRelated: trim)
    }
}
