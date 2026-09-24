import Foundation
import CoreFoundation

/// Local page anchors use the existing recoverable PDF transaction. The web
/// adapter carries a ticket only; it neither materializes nor mutates records.
struct ReaderNativePDFPageState {
    typealias O = [String:Any]
    let writer:ReaderNativeBookStore
    let device:ReaderNativeDataStore
    private var document:ReaderNativeDataStore { writer.store }
    private var book:String { writer.bookID }
    private var kind:String { "pdf-mutation-journal" }
    private var projection:ReaderNativeBookProjection { .init(store:document) }
    private static let kinds = ["reading-position","document-highlights","ink","document-notes-legacy","user-pages","card-placements","entity-references"]
    private static let policy = [
        ["pdf-highlights","local-migrate"],["reader-notes","local-migrate"],["reader-positions","local-migrate"],
        ["pdf-ink","local-migrate"],["reader-favorites","pi-preserve-rebind"],["reader-userpages","local-migrate"],
        ["pdf-tr-sentences","pi-preserve-rebind"],["pdf-char-offset","pi-preserve-rebind"],["pdf-toc-range","pi-preserve-rebind"],
        ["sentence-cards","pi-preserve-rebind"],["vocab-exposure","pi-preserve-rebind"],["pdf-figures","pi-preserve-rebind"],
        ["pdf-ocr-fix","native-ocr-migrate"],["pdf-page-ocr","native-ocr-migrate"],["ocr-checkpoints","native-preserve-reprocess"],
        ["vocab-lookups","pi-preserve-rebind"],["assistant-convo","pi-preserve-rebind"],["attention-dwell","pi-preserve-rebind"],
        ["attention-db","pi-preserve-rebuild"],["render-caches","pi-preserve-rebuild"]]
    private static let phases = ["prepared","document-applied","anchors-applied","pdf-replaced","committed"]
    private func failure(_ message:String) -> ReaderNativeBookStore.MutationError { .invalid("PDF 改页：" + message) }
    private func json(_ value:Any) throws -> Data { try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys,.fragmentsAllowed]) }
    private func same(_ a:Any,_ b:Any) throws -> Bool { try json(a) == json(b) }
    private func number(_ value:Any?) -> Int? {
        let n:Double?
        if let text = value as? String { n = Double(text) }
        else if let v = value as? NSNumber, CFGetTypeID(v) != CFBooleanGetTypeID() { n = v.doubleValue }
        else { n = nil }
        guard let n, n.isFinite, n.rounded() == n, abs(n) <= 9_007_199_254_740_990 else { return nil }
        return Int(n)
    }
    private func journal() throws -> (value:O,revision:Int64)? {
        let state = try projection.state(kind,bookID:book)
        guard let raw = state.payload else { return nil }
        guard let value = raw as? O,
              Set(value.keys) == Set(["contract","localBookId","ticket","phase","oldContentSHA256","stagedContentSHA256","plan","before","after","deviceBefore","deviceAfter","domainPolicy"]),
              value["contract"] as? String == "reader-native-pdf-mutation-web-journal/1", value["localBookId"] as? String == book,
              let ticket = value["ticket"] as? String, ticket.range(of:"^npmt_[a-f0-9]{32}$",options:.regularExpression) != nil,
              Self.phases.contains(value["phase"] as? String ?? ""),
              let before = value["before"] as? O, let after = value["after"] as? O,
              let deviceBefore = value["deviceBefore"] as? O, value["deviceAfter"] is O,
              let deviceRevision = number(deviceBefore["rev"]), deviceRevision >= 0, deviceBefore["payload"] is O,
              let plan = value["plan"] as? O, ["insert","delete","edit"].contains(plan["operation"] as? String ?? ""),
              try same(value["domainPolicy"] ?? NSNull(),Self.policy) else { throw failure("恢复记录损坏") }
        for key in ["oldContentSHA256","stagedContentSHA256"] {
            guard let hash = value[key] as? String, hash.range(of:"^[a-f0-9]{64}$",options:.regularExpression) != nil else { throw failure("恢复摘要无效") }
        }
        for key in Self.kinds {
            guard let row = before[key] as? O, let revision = number(row["rev"]), revision >= 0, row["payload"] != nil, after[key] != nil else { throw failure("恢复状态缺失") }
        }
        return (value,state.revision)
    }
    private func lease(_ value:O,_ revision:Int64) -> O {
        let header = value.filter { ["ticket","phase","oldContentSHA256","stagedContentSHA256"].contains($0.key) }
        return ["native":true,"journal":header,"journalRev":revision,"applied":value["phase"] as? String != "prepared"]
    }
    private func devicePosition() throws -> (payload:O,revision:Int64) {
        let id = writer.deviceID + ":reader-positions"
        guard let record = try device.record(collection:"native-reader-positions",id:id), !record.deleted else { return ([:],0) }
        guard let envelope = try JSONSerialization.jsonObject(with:Data(record.json.utf8)) as? O,
              let value = envelope["value"] as? O, value["id"] as? String == id, value["deviceId"] as? String == writer.deviceID,
              let payload = value["payload"] as? O else { throw failure("设备续读状态损坏") }
        return (payload,record.rev)
    }
    private func saveDevice(_ value:O,revision:Int64) throws {
        let collection = "native-reader-positions", id = writer.deviceID + ":reader-positions", at = writer.now()
        let mutation = "native-pdf-position-" + UUID().uuidString
        let row:O = ["schema":1,"collection":collection,"id":id,"rev":revision+1,"updatedAt":at,"updatedBy":writer.deviceID,"deleted":false,
            "value":["id":id,"deviceId":writer.deviceID,"payload":value,"updatedAt":at]]
        let encoded = String(decoding:try json(row),as:UTF8.self)
        _ = try device.commitWithinTransaction(record:.init(collection:collection,id:id,rev:revision+1,updatedAt:at,deleted:false,json:encoded),mutationId:mutation,
            journalJSON:{ cursor in String(decoding:try! json(["mutationId":mutation,"operation":"put","collection":collection,"record":row,"cursor":cursor]),as:UTF8.self) },expectedRev:revision,now:at)
    }
    private func phase(_ ticket:String,_ name:String) throws -> O {
        guard Self.phases.contains(name) else { throw failure("阶段无效") }
        return try document.inTransaction {
            guard let old = try journal(), old.value["ticket"] as? String == ticket else { throw failure("恢复票据不一致") }
            var value = old.value; value["phase"] = name
            let revision = try writer.writeState(kind,value:value,expected:old.revision,mutation:UUID().uuidString,at:writer.now())
            return lease(value,revision)
        }
    }
    func handle(_ request:O) throws -> O {
        switch request["operation"] as? String {
        case "read":
            return ["ok":true,"transaction":try journal().map { lease($0.value,$0.revision) } as Any? ?? NSNull()]
        case "prepare":
            guard let plan = request["plan"] as? O, let prepared = request["prepared"] as? O else { throw failure("准备参数缺失") }
            return ["ok":true,"transaction":try prepare(plan,prepared:prepared)]
        case "phase":
            guard let ticket = request["ticket"] as? String, let value = request["phase"] as? String else { throw failure("阶段参数缺失") }
            return ["ok":true,"transaction":try phase(ticket,value)]
        case "reconcile":
            guard let ticket = request["ticket"] as? String, let desired = request["desired"] as? String, ["before","after"].contains(desired) else { throw failure("恢复方向无效") }
            return ["ok":true,"transaction":try reconcile(ticket,desired:desired)]
        case "remove":
            guard let ticket = request["ticket"] as? String else { throw failure("恢复票据缺失") }
            try document.inTransaction {
                guard let old = try journal() else { return }
                guard old.value["ticket"] as? String == ticket else { throw failure("拒绝移除另一项恢复记录") }
                let id = book + ":" + kind, collection = "native-" + kind, at = writer.now(), mutation = "native-pdf-journal-remove-" + UUID().uuidString
                let row:O = ["schema":1,"collection":collection,"id":id,"rev":old.revision+1,"updatedAt":at,"updatedBy":writer.deviceID,"deleted":true,"value":NSNull()]
                let encoded = String(decoding:try json(row),as:UTF8.self)
                _ = try document.commitWithinTransaction(record:.init(collection:collection,id:id,rev:old.revision+1,updatedAt:at,deleted:true,json:encoded),mutationId:mutation,
                    journalJSON:{ cursor in String(decoding:try! json(["mutationId":mutation,"operation":"remove","collection":collection,"record":row,"cursor":cursor]),as:UTF8.self) },expectedRev:old.revision,now:at)
            }
            return ["ok":true]
        default: throw failure("未知操作")
        }
    }
    private func prepare(_ plan:O,prepared:O) throws -> O {
        guard let operation = plan["operation"] as? String, ["insert","delete","edit"].contains(operation),
              let pivot = number(plan["pivotPage"]), pivot > 0, let id = plan["id"] as? String, !id.isEmpty,
              let title = plan["title"] as? String, let markdown = plan["markdown"] as? String,
              title.utf16.count <= 120, markdown.utf16.count <= 100_000,
              prepared["operation"] as? String == operation, number(prepared["pivotPage"]) == pivot,
              let ticket = prepared["ticket"] as? String, ticket.range(of:"^npmt_[a-f0-9]{32}$",options:.regularExpression) != nil else { throw failure("页锚计划与 PDF 不一致") }
        func page(_ p:Int) -> Int? {
            if p < 1 { return p }
            if operation == "insert" { return p >= pivot ? p+1 : p }
            if operation == "delete" { return p == pivot ? nil : p > pivot ? p-1 : p }
            return p
        }
        func position(_ input:Any) -> Any {
            guard var value = input as? O, value["kind"] as? String != "epub", let p = number(value["pos"]) else { return input }
            value["pos"] = page(p) ?? max(1,p-1); value["ts"] = writer.now()/1000; return value
        }
        func anchored(_ item:O) -> O? {
            var item = item
            if var anchor = item["anchor"] as? O, anchor["kind"] as? String == "pdf", let p = anchor["page"] as? Int {
                guard let next = page(p) else { return nil }; anchor["page"] = next; item["anchor"] = anchor
            }
            return item
        }
        return try document.inTransaction {
            guard try journal() == nil else { throw failure("已有未恢复的改页") }
            let deviceState = try devicePosition()
            var before:O = [:], after:O = [:]
            for key in Self.kinds {
                if key == "document-highlights" {
                    let value = try projection.highlights(key,bookID:book); before[key] = ["kind":key,"payload":value.items,"rev":value.revision]
                } else {
                    let state = try projection.state(key,bookID:book)
                    let fallback:Any = key == "reading-position" ? NSNull() : key == "ink" ? O() : [O]()
                    before[key] = ["kind":key,"payload":state.payload ?? fallback,"rev":state.revision]
                }
            }
            func source(_ key:String) -> Any { (before[key] as! O)["payload"]! }
            guard let oldNotes = source("document-notes-legacy") as? [O], let highlights = source("document-highlights") as? [O],
                  let ink = source("ink") as? O, let oldPages = source("user-pages") as? [O], let oldPlacements = source("card-placements") as? [O] else { throw failure("原页锚状态损坏") }
            let notes = oldNotes.compactMap { original -> O? in
                guard var item = anchored(original) else { return nil }
                for slot in ["card","html"] {
                    guard var payload = item[slot] as? O, var bind = payload["bind"] as? O, let p = number(bind["page"]) else { continue }
                    if let next = page(p) { bind["page"] = next; payload["bind"] = bind } else { payload.removeValue(forKey:"bind") }
                    item[slot] = payload
                }
                return item
            }
            after["document-notes-legacy"] = notes
            after["document-highlights"] = highlights.compactMap { original -> O? in
                var item = original
                if let p = number(item["page"]) { guard let next = page(p) else { return nil }; item["page"] = next }
                return item
            }
            var movedInk:O = [:]
            for (key,value) in ink {
                var next = key
                if key.range(of:"^[1-9][0-9]*$",options:.regularExpression) != nil, let p = Int(key) {
                    guard let mapped = page(p) else { continue }; next = String(mapped)
                } else if key.range(of:"^pdf\\|.{1,512}\\|[1-9][0-9]*$",options:.regularExpression) != nil,
                          let separator = key.lastIndex(of:"|"), let p = Int(key[key.index(after:separator)...]) {
                    guard let mapped = page(p) else { continue }; next = String(key[...separator]) + String(mapped)
                }
                movedInk[next] = value
            }
            after["ink"] = movedInk
            after["reading-position"] = position(source("reading-position"))
            let priorIDs = Set(oldNotes.compactMap { $0["id"] as? String }), keptIDs = Set(notes.compactMap { $0["id"] as? String })
            var placements = oldPlacements.compactMap { item -> O? in
                if let id = item["noteId"] as? String, priorIDs.contains(id), !keptIDs.contains(id) { return nil }
                return anchored(item)
            }
            for row in ReaderNativeBookStore.placements(notes) {
                if let index = placements.firstIndex(where:{ $0["placementId"] as? String == row["placementId"] as? String }) { placements[index] = row }
                else { placements.append(row) }
            }
            after["card-placements"] = placements; after["entity-references"] = ReaderNativeBookStore.references(placements)
            var pages = oldPages.compactMap { original -> O? in
                var item = original
                if let p = item["page"] as? Int { guard let next = page(p) else { return nil }; item["page"] = next }
                else if let p = number(item["after"]) {
                    if operation == "insert", p > (number(plan["after"]) ?? pivot-1) { item["after"] = p+1 }
                    if operation == "delete", p >= pivot { item["after"] = max(0,p-1) }
                }
                return item
            }
            let now = writer.now()/1000
            if operation == "insert" { pages.append(["id":id,"page":pivot,"title":title,"md":markdown,"real":true,"mode":"overlay","md_ver":0,"synced_ver":0,"created":now,"updated":now]) }
            else if operation == "delete" { pages.removeAll { $0["id"] as? String == id } }
            else {
                guard let index = pages.firstIndex(where:{ $0["id"] as? String == id }) else { throw failure("待编辑用户页已消失") }
                let titleProvided = plan["titleProvided"] as? Bool == true, markdownProvided = plan["markdownProvided"] as? Bool == true
                if titleProvided { pages[index]["title"] = title }; if markdownProvided { pages[index]["md"] = markdown }
                if pages[index]["mode"] as? String == "overlay" {
                    let version = (number(pages[index]["md_ver"]) ?? 0) + (titleProvided || markdownProvided ? 1 : 0)
                    pages[index]["md_ver"] = version; pages[index]["synced_ver"] = version
                }
                pages[index]["updated"] = now
            }
            after["user-pages"] = pages
            var positions = deviceState.payload
            if let old = positions["localbook:" + book] { positions["localbook:" + book] = position(old) }
            let value:O = ["contract":"reader-native-pdf-mutation-web-journal/1","localBookId":book,"ticket":ticket,"phase":"prepared",
                "oldContentSHA256":prepared["oldContentSHA256"] ?? NSNull(),"stagedContentSHA256":prepared["stagedContentSHA256"] ?? NSNull(),
                "plan":plan,"before":before,"after":after,"deviceBefore":["payload":deviceState.payload,"rev":deviceState.revision],"deviceAfter":positions,"domainPolicy":Self.policy]
            let revision = try writer.writeState(kind,value:value,mutation:ticket + ":prepare",at:writer.now())
            _ = try journal() // Validate before the transaction is allowed to commit.
            return lease(value,revision)
        }
    }
    private func reconcile(_ ticket:String,desired:String) throws -> O {
        let transaction = try document.inTransaction { () throws -> O in
            guard let state = try journal(), state.value["ticket"] as? String == ticket else { throw failure("恢复票据不一致") }
            let before = state.value["before"] as! O, after = state.value["after"] as! O
            let mutation = UUID().uuidString, at = writer.now()
            for key in Self.kinds {
                let old = (before[key] as! O)["payload"]!, next = after[key]!, wanted = desired == "after" ? next : old
                let current:(revision:Int64,payload:Any?)
                if key == "document-highlights" { let high = try projection.highlights(key,bookID:book); current = (high.revision,high.items) }
                else { current = try projection.state(key,bookID:book) }
                let existing = current.payload ?? old
                guard try same(existing,old) || same(existing,next) else { throw failure("检测到并发页锚写入：" + key) }
                if try same(existing,wanted) { continue }
                if key == "document-highlights" {
                    guard let items = wanted as? [O] else { throw failure("划线状态损坏") }
                    _ = try writer.writeHighlights(key,items:items,expected:current.revision,mutation:mutation,at:at)
                } else {
                    try writer.writeState(key,value:wanted,expected:current.revision,mutation:mutation,at:at)
                    if key == "document-notes-legacy", let notes = wanted as? [O] {
                        try writer.writeState("word-bindings",value:ReaderNativeBookStore.wordBindings(notes),mutation:mutation,at:at)
                    }
                }
            }
            _ = try phase(ticket,desired == "after" ? "document-applied" : "prepared")
            return state.value
        }
        // Device and document databases keep the old recoverable two-phase
        // contract. If this second transaction fails, the durable journal
        // remains and reopening reconciles before/after without reapplying.
        try device.inTransaction {
            let old = (transaction["deviceBefore"] as! O)["payload"]!, next = transaction["deviceAfter"]!
            let current = try devicePosition(), wanted = desired == "after" ? next : old
            guard try same(current.payload,old) || same(current.payload,next) else { throw failure("检测到并发设备位置写入") }
            if try !same(current.payload,wanted) { try saveDevice(wanted as! O,revision:current.revision) }
        }
        return try phase(ticket,desired == "after" ? "anchors-applied" : "prepared")
    }
}
