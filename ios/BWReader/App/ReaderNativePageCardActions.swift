import Foundation

/// Native owner of the existing durable page-card saga. Card entities and book
/// placements live in different databases: persist intent before changing either,
/// then resume the same operation after interruption instead of inventing a new ID.
struct ReaderNativePageCardActions {
    typealias O = [String:Any]
    typealias F = ReaderNativeAssistantEdits.Failure
    let book: ReaderNativeBookStore
    let repository: ReaderNativeCardRepository
    var sanitizeHTML: ReaderNativePageCardPlan.HTML = { _ in throw F("原生 HTML 净化器未就绪") }
    private var projection: ReaderNativeBookProjection { .init(store:book.store) }
    private var stamp: Int64 { book.now() }
    private var file: String { "localbook:" + book.bookID }

    func perform(_ input: O) throws -> O {
        let cursor = try repository.store.cursor()
        let result: O
        switch input["operation"] as? String {
        case "action":
            guard let data = input["data"] as? O, let authority = input["expectedState"] as? O else { throw F("页面卡片动作缺少上下文") }
            result = try apply(ReaderNativePageCardPlan.action(data,authority:authority,file:file,now:stamp,sanitize:sanitizeHTML))
        case "direct": result = try direct(input)
        case "apply": result = try apply(input)
        case "transition": result = try transition(input)
        case "recover":
            var recovered = false
            while let pending = try records().receipts.first(where: { $0["contract"] as? String == Self.contract && ($0["state"] as? String == "preparing" || $0["pending"] is O) }) {
                _ = try finish(pending, recovering:true); recovered = true
            }
            result = ["recovered":recovered]
        default: throw F("未知页面卡片操作")
        }
        let changes = try repository.store.journal(after:cursor,limit:4096).map { try JSONSerialization.jsonObject(with:Data($0.json.utf8)) }
        return ["ok":true,"result":result,"changes":changes]
    }
    private static let contract = "reader-native-page-card-action/1"
    private struct Records { let notes:[O]; let noteRevision:Int64; let receipts:[O]; let receiptRevision:Int64 }
    private func records() throws -> Records {
        try book.store.inTransaction {
            let n = try projection.state("document-notes-legacy",bookID:book.bookID), r = try projection.state("pdf-assistant-ops",bookID:book.bookID)
            guard (n.payload == nil || n.payload is [O]), (r.payload == nil || r.payload is [O]) else { throw F("页面卡片日志损坏") }
            return Records(notes:n.payload as? [O] ?? [],noteRevision:n.revision,receipts:r.payload as? [O] ?? [],receiptRevision:r.revision)
        }
    }
    private func save(_ journal: O, previous: O?, notes: [O]? = nil, state: Records) throws -> Int64 {
        try book.store.inTransaction {
            let current = try projection.state("pdf-assistant-ops",bookID:book.bookID)
            guard current.revision == state.receiptRevision else { throw F("页面卡片日志已经变化",conflict:true) }
            var receipts = state.receipts
            let index = receipts.firstIndex { $0["id"] as? String == journal["id"] as? String }
            if let previous {
                guard let index, same(receipts[index],previous) else { throw F("页面卡片日志不一致",conflict:true) }
                receipts[index] = journal
            } else {
                guard index == nil else { throw F("页面卡片操作编号已存在",conflict:true) }
                receipts.append(journal)
            }
            let mutation = "page-card:" + UUID().uuidString
            var revision = state.noteRevision
            if let notes { revision = try book.writeNotes(notes,expected:state.noteRevision,mutation:mutation,at:stamp) }
            try book.writeState("pdf-assistant-ops",value:ReaderNativeHighlightRules.boundedReceipts(receipts),expected:state.receiptRevision,mutation:mutation + ":ops",at:stamp)
            return revision
        }
    }
    private func operationID(_ raw: Any?) throws -> String {
        guard let id = raw as? String, id.range(of:"^(npdf|pcard)_[0-9a-f]{24}$",options:.regularExpression) != nil else { throw F("页面卡片操作编号无效") }
        return id
    }
    private func integer(_ value: Any?) throws -> Int64 { try ReaderNativeCardRules.integer(value,"页面卡片版本或位置") }
    private func same(_ lhs: Any?, _ rhs: Any?) -> Bool { ReaderNativeCardRules.same(lhs ?? NSNull(),rhs ?? NSNull()) }
    private func sameFingerprint(_ lhs: Any?, _ rhs: String) -> Bool {
        guard let lhs = lhs as? String else { return false }
        if lhs == rhs { return true }
        guard let a = try? JSONSerialization.jsonObject(with:Data(lhs.utf8)), let b = try? JSONSerialization.jsonObject(with:Data(rhs.utf8)) else { return false }
        return same(a,b)
    }
    private func snapshot(_ value: Any?, id: String) throws -> O {
        guard let value = value as? O else { throw F("页面卡片快照缺失") }
        return try ReaderNativeAssistantEdits.note(value,fallbackID:id,file:file,now:stamp)
    }
    private func identity(_ note: O) -> O {
        var value = note; value.removeValue(forKey:"updated")
        for kind in ["card","html"] {
            if var content = value[kind] as? O {
                for key in [kind == "card" ? "cards" : "content","contextText","context_text"] { content.removeValue(forKey:key) }
                value[kind] = content
            }
        }
        return value
    }
    private func loadEntity(_ id: String) throws -> O {
        let reply = try repository.perform(["operation":"load","arguments":[id]])
        guard let record = reply["result"] as? O, record["deleted"] as? Bool != true, record["cards"] is [O] else { throw F("统一学习卡实体不存在",conflict:true) }
        return record
    }
    private func entityIntent(plan: O, before: O, id: String) throws -> O? {
        guard let cards = plan["replacementCards"] as? [O], let canonicalID = plan["canonicalId"] as? String else { return nil }
        let record = try loadEntity(canonicalID), old = record["cards"] as! [O]
        guard let face = before["card"] as? O, same(face["cards"],old), cards.count == old.count else { throw F("页面副本与学习卡实体不一致",conflict:true) }
        let merged = cards.enumerated().map { index, face -> O in
            var next = face; for field in ["deck","tags","reason"] { if let value = old[index][field] { next[field] = value } }; return next
        }
        return ["id":canonicalID,"beforeCards":old,"afterCards":merged,"beforeEntityRev":try integer(record["entityRev"]),"lastEntityRev":NSNull(),"mutationId":id + ":entity:apply"]
    }
    private func apply(_ input: O) throws -> O {
        let id = try operationID(input["operationId"])
        guard let plan = input["plan"] as? O, let placement = plan["id"] as? String,
              let kind = input["kind"] as? String, ["page-card-edit","page-card-delete"].contains(kind),
              let fingerprint = input["fingerprint"] as? String, let requestFingerprint = input["requestFingerprint"] as? String,
              let authority = input["expectedState"] as? O else { throw F("页面卡片操作资料不完整") }
        let state = try records()
        if let previous = state.receipts.first(where: { $0["id"] as? String == id }) {
            guard previous["contract"] as? String == Self.contract, sameFingerprint(previous["fingerprint"],fingerprint),
                  sameFingerprint(previous["requestFingerprint"],requestFingerprint) else { throw F("页面卡片操作编号冲突",conflict:true) }
            if previous["state"] as? String == "done", !(previous["pending"] is O) { return ["receipt":previous,"revision":state.noteRevision,"replayed":true] }
            guard previous["state"] as? String == "preparing" else { throw F("该操作已撤销或发生冲突",conflict:true) }
            return try finish(previous,recovering:false)
        }
        let before = try snapshot(plan["before"],id:placement), expected = try integer(plan["expectedRevision"]), page = try integer(plan["page"])
        guard page > 0, let position = state.notes.firstIndex(where: { $0["id"] as? String == placement }),
              same(try snapshot(state.notes[position],id:placement),before) else { throw F("页面卡片内容已经变化",conflict:true) }
        let stable = plan["number"] == nil || plan["number"] is NSNull || authority["page_card_stable_id"] as? Bool == true
        let revisions = authority["revisions"] as? O ?? authority
        if !stable { guard try integer(revisions["notes"]) == expected, state.noteRevision == expected else { throw F("页面卡片序号已经变化",conflict:true) } }
        if let projected = authority["page_cards"] as? O {
            let rows = (projected["pages"] as? O)?[String(page)] as? [O]
            guard projected["contract"] as? String == "reader-native-page-card-projection/1",
                  let row = rows?.first(where: { $0["id"] as? String == placement }),
                  stable || (same(row["number"],plan["number"]) && row["unbound"] as? Bool != true && (try? integer(projected["revision"])) == expected) else {
                throw F("页面卡片投影已变化",conflict:true)
            }
        }
        var after = kind == "page-card-edit" ? try snapshot(plan["after"],id:placement) : nil
        guard before["video"] == nil || before["video"] is NSNull,
              (before["card"] is O) != (before["html"] is O),
              after == nil || same(identity(before),identity(after!)) else { throw F("页面卡片修改越过内容边界",conflict:true) }
        let entity = try entityIntent(plan:plan,before:before,id:id)
        if let entity, var card = after?["card"] as? O { card["cards"] = entity["afterCards"]; after?["card"] = card }
        let journal: O = ["id":id,"contract":Self.contract,"kind":kind,"state":"preparing","fingerprint":fingerprint,
            "requestFingerprint":requestFingerprint,"placementId":placement,"page":page,"number":plan["number"] ?? NSNull(),
            "expectedRevision":expected,"before":before,"after":after as Any? ?? NSNull(),"beforeIndex":position,
            "entity":entity as Any? ?? NSNull(),"transition":0,"ts":stamp / 1000]
        _ = try save(journal,previous:nil,state:state)
        return try finish(journal,recovering:false)
    }
    private func direct(_ request: O) throws -> O {
        typealias P = ReaderNativePageCardPlan
        guard let input = request["input"] as? O, let operation = input["operation"] as? String,
              ["edit","delete"].contains(operation), let id = input["expectedId"] as? String else { throw F("页面卡片修改资料不完整") }
        var keys: Set<String> = ["operation","operationId","expectedId","expectedRevision"]
        if operation == "edit" { keys.insert("replacement") }; if input["number"] != nil { keys.insert("number") }
        try P.fields(input,keys)
        let op = try operationID(input["operationId"]), expected = try P.integer(input["expectedRevision"])
        let number: Int64? = input["number"] == nil ? nil : try P.integer(input["number"],min:1,max:1_000_000)
        var replacement: O?
        if operation == "edit" {
            guard let value = input["replacement"] as? O else { throw F("页面卡片替换内容缺失") }
            if Set(value.keys) == ["cards"] { replacement = ["cards":try P.cards(value["cards"])] }
            else if Set(value.keys) == ["content"] { replacement = ["content":try sanitizeHTML(P.text(value["content"])).content] }
            else { throw F("页面卡片替换字段无效") }
        }
        let fingerprint = try P.requestFingerprint(operation:operation,id:id,revision:expected,number:number,replacement:replacement)
        let state = try records()
        if let prior = state.receipts.first(where: { $0["id"] as? String == op }) {
            guard prior["contract"] as? String == Self.contract, sameFingerprint(prior["requestFingerprint"],fingerprint) else { throw F("页面卡片操作编号冲突",conflict:true) }
            if prior["state"] as? String == "done", !(prior["pending"] is O) { return directResult(prior,operation:operation,replayed:true) }
            guard prior["state"] as? String == "preparing" else { throw F("页面卡片操作已经撤销或冲突",conflict:true) }
            let finished = try finish(prior,recovering:false)
            return directResult(finished["receipt"] as! O,operation:operation,replayed:false)
        }
        // An already committed delete may no longer have a visible page/row.
        // Its durable receipt above remains authoritative for exact replay.
        guard let projection = request["projection"] as? O,
              projection["contract"] as? String == "reader-local-page-card-projection/1",
              let rows = projection["cards"] as? [O] else { throw F("当前页卡片精确序号不可用") }
        let page = try P.integer(projection["page"],min:1,max:10_000_000)
        guard try P.integer(projection["revision"]) == state.noteRevision,
              let stored = state.notes.first(where:{ $0["id"] as? String == id }),
              let row = rows.first(where:{ number == nil ? $0["id"] as? String == id : ($0["number"] as? NSNumber)?.int64Value == number }),
              row["id"] as? String == id, number == nil || row["unbound"] as? Bool != true else { throw F("页面卡片投影或序号已经变化",conflict:true) }
        let before = try snapshot(stored,id:id)
        if expected != state.noteRevision {
            guard number == nil, let cached = request["cachedSnapshot"] as? String,
                  let decoded = try? JSONSerialization.jsonObject(with:Data(cached.utf8)), same(decoded,before) else { throw F("页面卡片列表已经变化",conflict:true) }
        }
        var item: O = ["id":id,"before":before]
        if let replacement {
            var after = before
            let slot = before["card"] is O ? "card" : "html"
            guard var face = after[slot] as? O, (slot == "card") == (replacement["cards"] != nil) else { throw F("替换内容与卡片类型不匹配") }
            for (key,value) in replacement { face[key] = value }; after[slot] = face; after["updated"] = stamp / 1000; item["after"] = after
        }
        let data: O = ["type":"page-card","op":operation,"native_operation_id":op,"file":file,"page":page,
            "number":row["unbound"] as? Bool == true ? NSNull() : row["number"] ?? NSNull(),"expected_id":id,"expected_revision":expected,"item":item]
        let authority: O = ["revisions":["notes":state.noteRevision],"page_card_stable_id":number == nil,
            "page_cards":["contract":"reader-native-page-card-projection/1","revision":state.noteRevision,"pages":[String(page):rows]]]
        let prepared = try P.action(data,authority:authority,file:file,now:stamp,sanitize:sanitizeHTML)
        let finished = try apply(prepared)
        return directResult(finished["receipt"] as! O,operation:operation,replayed:finished["replayed"] as? Bool == true)
    }
    private func directResult(_ journal: O, operation: String, replayed: Bool) -> O {
        ["ok":true,"operationId":journal["id"]!,"operation":operation,"page":journal["page"]!,
         "number":journal["number"] ?? NSNull(),"id":journal["placementId"]!,"replayed":replayed]
    }
    private func transition(_ input: O) throws -> O {
        let id = try operationID(input["operationId"])
        guard let action = input["action"] as? String, ["undo","redo"].contains(action) else { throw F("页面卡片撤销操作无效") }
        let state = try records(), target = action == "undo" ? "undone" : "done", source = action == "undo" ? "done" : "undone"
        guard let old = state.receipts.first(where: { $0["id"] as? String == id }), old["contract"] as? String == Self.contract else { throw F("页面卡片操作记录已失效",conflict:true) }
        if old["state"] as? String == target, !(old["pending"] is O) { return ["receipt":old,"revision":state.noteRevision,"replayed":true] }
        var next = old
        if let pending = old["pending"] as? O {
            guard pending["target"] as? String == target else { throw F("卡片撤销状态冲突",conflict:true) }
        } else {
            guard old["state"] as? String == source else { throw F("卡片撤销状态冲突",conflict:true) }
            next["transition"] = try integer(old["transition"] ?? 0) + 1
            next["pending"] = ["target":target,"step":"intent"]; next["updatedAt"] = stamp
            _ = try save(next,previous:old,state:state)
        }
        return try finish(next,recovering:false)
    }
    private func transitionEntity(_ raw: Any?, target: String, journal: O, compensate: Bool = false) throws -> Any {
        guard var entity = raw as? O else { return NSNull() }
        guard let id = entity["id"] as? String, let before = entity["beforeCards"] as? [O], let after = entity["afterCards"] as? [O] else { throw F("学习卡恢复日志损坏") }
        let current = try loadEntity(id), wanted = target == "done" ? after : before, other = target == "done" ? before : after
        if same(current["cards"],wanted) { entity["lastEntityRev"] = current["entityRev"]; return entity }
        guard same(current["cards"],other) else { throw F("学习卡内容已经变化",conflict:true) }
        let revision = try integer(current["entityRev"]), initial = journal["state"] as? String == "preparing" && !compensate
        if initial { guard revision == (try integer(entity["beforeEntityRev"])) else { throw F("学习卡版本已经变化",conflict:true) } }
        let operation = try operationID(journal["id"]), transition = try integer(journal["transition"] ?? 0) + (compensate ? 1_000_000 : 0)
        let mutation = initial ? operation + ":entity:apply" : operation + ":t\(transition):entity"
        let reply = try repository.perform(["operation":"replaceContent","arguments":[id,wanted,["ifEntityRev":revision,"mutationId":mutation]],"mutationId":mutation])
        guard let result = reply["result"] as? O else { throw F("学习卡更新回执缺失") }
        entity["lastEntityRev"] = try integer(result["entityRev"]); return entity
    }
    private func finish(_ journal: O, recovering: Bool) throws -> O {
        let id = try operationID(journal["id"]), preparing = journal["state"] as? String == "preparing"
        let target = preparing ? "done" : (journal["pending"] as? O)?["target"] as? String ?? ""
        guard ["done","undone"].contains(target), let placement = journal["placementId"] as? String,
              let kind = journal["kind"] as? String, ["page-card-edit","page-card-delete"].contains(kind) else { throw F("页面卡片恢复状态无效") }
        let before = try snapshot(journal["before"],id:placement), after = kind == "page-card-edit" ? try snapshot(journal["after"],id:placement) : nil
        let state = try records()
        guard let current = state.receipts.first(where: { $0["id"] as? String == id }), same(current,journal) else { throw F("页面卡片恢复日志已变化",conflict:true) }
        var notes = state.notes
        let index = notes.firstIndex { $0["id"] as? String == placement }
        let currentNote = try index.map { try snapshot(notes[$0],id:placement) }
        let wanted: O? = kind == "page-card-delete" ? (target == "done" ? nil : before) : (target == "done" ? after : before)
        let other: O? = kind == "page-card-delete" ? (target == "done" ? before : nil) : (target == "done" ? before : after)
        if !same(currentNote,wanted) && !same(currentNote,other) {
            if recovering {
                var failed = journal; failed["state"] = "conflicted"; failed["pending"] = NSNull(); failed["recoveryError"] = "placement-content-conflict"; failed["updatedAt"] = stamp
                _ = try save(failed,previous:journal,state:state)
                _ = try? transitionEntity(journal["entity"],target:target == "done" ? "undone" : "done",journal:journal,compensate:true)
                return ["receipt":failed,"revision":state.noteRevision,"replayed":false]
            }
            throw F("页面卡片内容已经变化",conflict:true)
        }
        var next = journal
        next["entity"] = try transitionEntity(journal["entity"],target:target,journal:journal)
        let changed = !same(currentNote,wanted)
        if changed {
            if let index { if let wanted { notes[index] = wanted } else { notes.remove(at:index) } }
            else if let wanted { notes.insert(wanted,at:min(notes.count,Int(try integer(journal["beforeIndex"] ?? 0)))) }
        }
        next["state"] = target; next["pending"] = NSNull(); next["updatedAt"] = stamp
        let revision = try save(next,previous:journal,notes:changed ? notes : nil,state:state)
        return ["receipt":next,"revision":revision,"replayed":false]
    }
}
