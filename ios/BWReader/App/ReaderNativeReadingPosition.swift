import Foundation
import CoreFoundation

/// Device viewport geometry and the replicated page remain distinct: scrolling
/// within a page updates only local viewport state, not the command queue.
enum ReaderNativeReadingPosition {
    /// Called within the existing outgoing producer lane: focus/drawing and
    /// page text share one durable sequence, including across book switches.
    static func publishContext(_ input:[String:Any],store:ReaderNativeDataStore,bookID:String,deviceID:String) throws -> [String:Any] {
        func bad() -> ReaderNativeBookStore.MutationError { .invalid("阅读上下文或发送队列") }
        func integer(_ value:Any?,min:Double = 0) -> Int64? {
            guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite,
                  n.doubleValue.rounded() == n.doubleValue, (min...9_007_199_254_740_990).contains(n.doubleValue) else { return nil }
            return n.int64Value
        }
        func string(_ value:Any?,limit:Int,multiline:Bool = false) -> String? {
            guard let s = value as? String, s.utf16.count <= limit,
                  s.unicodeScalars.allSatisfy({ $0.value >= 32 && $0.value != 127 || multiline && [9,10,13].contains($0.value) }) else { return nil }
            return s
        }
        guard !deviceID.isEmpty, deviceID.utf16.count <= 240,
              Set(input.keys) == Set(["kind","file","page","title","text","textAvailable","textSource","fallbackReason","truncated"]),
              input["file"] as? String == "localbook:" + bookID,
              let kind = input["kind"] as? String, ["pdf","epub"].contains(kind), let page = integer(input["page"]),
              let title = string(input["title"],limit:1024), let text = string(input["text"],limit:220_000,multiline:true),
              let available = input["textAvailable"] as? NSNumber, CFGetTypeID(available) == CFBooleanGetTypeID(),
              available.boolValue == !text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,
              let truncated = input["truncated"] as? NSNumber, CFGetTypeID(truncated) == CFBooleanGetTypeID(),
              let source = input["textSource"] as? String, source.range(of:"^[a-z][a-z0-9._-]{0,95}$",options:.regularExpression) != nil,
              input["fallbackReason"] is NSNull || string(input["fallbackReason"],limit:400) != nil else { throw bad() }
        return try store.inTransaction {
            let collection = "native-outgoing-journal", id = deviceID + ":outgoing-journal"
            let old = try store.record(collection:collection,id:id)
            var next:Int64 = 1, events:[[String:Any]] = []
            if let old, !old.deleted {
                guard let envelope = try JSONSerialization.jsonObject(with:Data(old.json.utf8)) as? [String:Any],
                      let value = envelope["value"] as? [String:Any], value["id"] as? String == id,
                      value["deviceId"] as? String == deviceID, let payload = value["payload"] as? [String:Any],
                      Set(payload.keys) == Set(["contract","nextSeq","events"]),
                      payload["contract"] as? String == "reader-native-outgoing-journal/1",
                      let saved = integer(payload["nextSeq"],min:1), let rows = payload["events"] as? [[String:Any]], rows.count <= 200 else { throw bad() }
                next = saved; events = rows
                var previous:Int64?
                for row in rows {
                    guard let seq = integer(row["seq"],min:1), previous == nil || seq == previous! + 1,
                          integer(row["v"]) == 1, integer(row["ts"]) != nil,
                          let type = row["type"] as? String, ["page.context","focus","drawing","command","command-failed"].contains(type),
                          let key = row["id"] as? String, key.range(of:"^[0-9a-f]{16}$",options:.regularExpression) != nil else { throw bad() }
                    previous = seq
                }
                if let previous, next != previous + 1 { throw bad() }
            }
            let stamp = Int64(Date().timeIntervalSince1970 * 1000)
            let eventID = String(UUID().uuidString.replacingOccurrences(of:"-",with:"").lowercased().prefix(16))
            let file = "localbook:" + bookID
            events.append(["v":1,"seq":next,"type":"page.context","ts":stamp/1000,"id":eventID,
                "event":"page.context","stable":true,"book_id":file,"file":file,"kind":kind,"page":page,"title":title,"text_available":available.boolValue,
                "page_context":["reason":"app-local-visible-window","text":text,"text_available":available.boolValue,
                    "text_source":source,"fallback_reason":input["fallbackReason"] ?? NSNull(),"truncated":truncated.boolValue,
                    "visual":NSNull(),"embeds":["highlights":0,"blocks":0,"unanchored":[]]]])
            let revision = (old?.rev ?? 0) + 1
            let payload:[String:Any] = ["contract":"reader-native-outgoing-journal/1","nextSeq":next+1,"events":Array(events.suffix(200))]
            let record:[String:Any] = ["schema":1,"collection":collection,"id":id,"rev":revision,"updatedAt":stamp,"updatedBy":deviceID,"deleted":false,
                "value":["id":id,"deviceId":deviceID,"payload":payload,"updatedAt":stamp]]
            let mutation = "native-page-context-" + eventID
            let change:[String:Any] = ["mutationId":mutation,"operation":"put","collection":collection,"record":record]
            func json(_ value:[String:Any]) throws -> String { String(decoding:try JSONSerialization.data(withJSONObject:value,options:.sortedKeys),as:UTF8.self) }
            let encoded = try json(record)
            _ = try store.commitWithinTransaction(record:.init(collection:collection,id:id,rev:revision,updatedAt:stamp,deleted:false,json:encoded),
                mutationId:mutation,journalJSON:{ cursor in
                    var result = change; result["cursor"] = cursor
                    return try! json(result)
                },expectedRev:old?.rev ?? 0,now:stamp)
            return ["ok":true,"contract":"reader-outgoing-context/1","seq":next,"eventId":eventID]
        }
    }

    /// Preserve the existing cross-book device index. The document position is
    /// authoritative; re-running after an interrupted cache write is harmless.
    static func cache(document:ReaderNativeDataStore,device:ReaderNativeDataStore,bookID:String,deviceID:String) throws {
        guard let position = try ReaderNativeBookProjection(store:document).state("reading-position",bookID:bookID).payload as? [String:Any] else { return }
        try device.inTransaction {
            let collection = "native-reader-positions", id = deviceID + ":reader-positions", file = "localbook:" + bookID
            let old = try device.record(collection:collection,id:id)
            var positions:[String:Any] = [:]
            if let old, !old.deleted {
                guard let envelope = try JSONSerialization.jsonObject(with:Data(old.json.utf8)) as? [String:Any],
                      let value = envelope["value"] as? [String:Any], value["id"] as? String == id,
                      value["deviceId"] as? String == deviceID, let payload = value["payload"] as? [String:Any] else {
                    throw ReaderNativeBookStore.MutationError.invalid("设备续读索引")
                }
                positions = payload
            }
            if let previous = positions[file] as? [String:Any], (previous["ts"] as? Double ?? 0) > (position["ts"] as? Double ?? 0) { return }
            if let previous = positions[file] as? NSDictionary, previous.isEqual(to:position) { return }
            positions[file] = position
            let at = Int64(Date().timeIntervalSince1970 * 1000), rev = (old?.rev ?? 0) + 1
            let value:[String:Any] = ["schema":1,"collection":collection,"id":id,"rev":rev,"updatedAt":at,"updatedBy":deviceID,"deleted":false,
                "value":["id":id,"deviceId":deviceID,"payload":positions,"updatedAt":at]]
            let json = String(decoding:try JSONSerialization.data(withJSONObject:value,options:.sortedKeys),as:UTF8.self)
            _ = try device.commitWithinTransaction(record:.init(collection:collection,id:id,rev:rev,updatedAt:at,deleted:false,json:json),
                mutationId:nil,journalJSON:nil,expectedRev:old?.rev ?? 0,now:at)
        }
    }

    static func validated(_ value:[String:Any]) throws -> [String:Any] {
        func number(_ key:String,_ range:ClosedRange<Double>,integer:Bool = false) throws -> Double {
            guard let n = value[key] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
                  n.doubleValue.isFinite, range.contains(n.doubleValue), !integer || n.doubleValue.rounded() == n.doubleValue else {
                throw ReaderNativeBookStore.MutationError.invalid("阅读位置 " + key)
            }
            return n.doubleValue
        }
        let page = try number("page",1...10_000_000,integer:true), fraction = try number("fraction",0...1), scale = try number("scale",0.001...100)
        let offset = try number("spreadOffset",0...1,integer:true)
        guard let mode = value["mode"] as? String, ["single","continuous","spread"].contains(mode),
              let flag = value["cropEnabled"] as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else {
            throw ReaderNativeBookStore.MutationError.invalid("阅读排版模式")
        }
        var result:[String:Any] = ["page":Int(page),"fraction":fraction,"scale":scale,"mode":mode,"spreadOffset":Int(offset),"cropEnabled":flag.boolValue]
        if flag.boolValue {
            guard let crop = value["crop"] as? [String:Any], Set(crop.keys) == Set(["l","r","t","b"]),
                  crop.values.allSatisfy({ raw in
                    guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
                    return number.doubleValue.isFinite && (0...45).contains(number.doubleValue)
                  }) else { throw ReaderNativeBookStore.MutationError.invalid("去边参数") }
            result["crop"] = crop
        }
        return result
    }

    static func restore(store:ReaderNativeDataStore,bookID:String,total:Int) throws -> [String:Any]? {
        guard total > 0 else { return nil }
        let read = ReaderNativeBookProjection(store:store)
        return try store.inTransaction {
            guard let raw = try read.state("pdf-viewport",bookID:bookID).payload as? [String:Any] else { return nil }
            var viewport = try validated(raw)
            let page = viewport["page"] as! Int
            let position = try read.state("reading-position",bookID:bookID).payload as? [String:Any]
            let saved = (position?["pos"] as? NSNumber)?.intValue ?? page
            let resolved = min(total,max(1,saved))
            if resolved != page { viewport["fraction"] = 0 }
            viewport["page"] = resolved; viewport["total"] = total; viewport["file"] = "localbook:" + bookID
            return viewport
        }
    }
}
