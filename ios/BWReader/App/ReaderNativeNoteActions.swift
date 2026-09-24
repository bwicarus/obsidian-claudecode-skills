import Foundation
import CoreFoundation

/// Card placement commands operate on the latest stored note. HTML/card
/// identity and fields unrelated to the command are retained byte-for-value.
enum ReaderNativeNoteActions {
    struct Request { let method: String; let body: [String:Any] }
    static func slot(_ note: [String:Any]) -> String? {
        if note["card"] is [String:Any] { return "card" }
        if note["html"] is [String:Any] { return "html" }
        if note["video"] is [String:Any] { return "video" }
        return nil
    }
    static func binding(_ note: [String:Any]) -> [String:Any]? {
        guard let slot = slot(note), let value = (note[slot] as? [String:Any])?["bind"] as? [String:Any],
              value["kind"] as? String == "page-chars" else { return nil }
        return value
    }
    static func geometry(_ note: [String:Any]) -> [Any] {
        [note["anchor"] ?? NSNull(),note["w"] ?? NSNull(),note["h"] ?? NSNull(),binding(note).map { $0 as Any } ?? NSNull(),NSNull()]
    }
    static func request(_ input: [String:Any], note: [String:Any], file:String, now:Int64) throws -> Request {
        guard let id = note["id"] as? String, input["id"] as? String == id,
              let action = input["action"] as? String else { throw ReaderNativeNoteRules.NoteError.invalid("卡片身份") }
        var body: [String:Any] = ["file":file,"id":id]
        if action == "remove" { return Request(method:"DELETE",body:body) }
        if action == "video" {
            guard var video = note["video"] as? [String:Any], let patch = input["changes"] as? [String:Any],
                  let id = video["id"] as? String, patch["id"] as? String == id,
                  Set(patch.keys).isSubset(of: ["id","start","end","rate","loop","cc"]) else {
                throw ReaderNativeNoteRules.NoteError.invalid("视频已改变")
            }
            for (key,value) in patch {
                if ["start","end","rate"].contains(key) {
                    guard let n = ReaderNativeStrokeRules.numeric(value), n >= 0, n <= 100_000 else { throw ReaderNativeNoteRules.NoteError.invalid("视频时间或速度") }
                } else if ["loop","cc"].contains(key) {
                    guard let flag = value as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else { throw ReaderNativeNoteRules.NoteError.invalid("视频开关") }
                }
                video[key] = value
            }
            body["video"] = video
            return Request(method:"PATCH",body:body)
        }
        if action == "ink" {
            guard slot(note) != nil, let raw = input["geometry"] as? String,
                  let data = raw.data(using:.utf8), let supplied = try? JSONSerialization.jsonObject(with:data),
                  try bytes(supplied) == bytes(geometry(note)) else {
                throw ReaderNativeNoteRules.NoteError.invalid("卡片位置已改变，请在新位置重画")
            }
            let value = try cardInk(input,note:note,now:now)
            body["strokes"] = value.strokes; body["iar"] = value.ratio
        } else if action == "update" {
            guard let changes = input["changes"] as? [String:Any],
                  Set(changes.keys).isSubset(of:["anchor","bind","form","w","h"]) else {
                throw ReaderNativeNoteRules.NoteError.invalid("卡片操作")
            }
            let name = slot(note)
            var payload = name.flatMap { note[$0] as? [String:Any] }
            var changedPayload = false
            if let anchor = changes["anchor"] as? [String:Any] {
                guard let page = integer(anchor["page"]), page > 0, page < 10_000_000,
                      let x = coordinate(anchor["x"]), let y = coordinate(anchor["y"]) else {
                    throw ReaderNativeNoteRules.NoteError.invalid("落点不在页面内")
                }
                body["anchor"] = ["kind":"pdf","page":page,"x":x,"y":y]
            }
            if let rawBind = changes["bind"] as? [String:Any], payload != nil {
                guard rawBind["kind"] as? String == "page-chars", let page = integer(rawBind["page"]), page > 0,
                      let from = integer(rawBind["from"]), let to = integer(rawBind["to"]), from >= 0, to >= from,
                      let text = rawBind["text"] as? String, !text.isEmpty else { throw ReaderNativeNoteRules.NoteError.invalid("词锚") }
                var bind: [String:Any] = ["kind":"page-chars","page":page,"from":from,"to":to,"text":String(text.prefix(200))]
                if let raw = rawBind["ois"] as? [Any], !raw.isEmpty, raw.count <= 512 {
                    let indexes = raw.compactMap(integer).filter { $0 >= 0 }
                    if !indexes.isEmpty { bind["ois"] = indexes }
                }
                payload?["bind"] = bind; changedPayload = true
            }
            // A nil/missing bind after dragging onto unreadable text retains
            // the previous word anchor; absence of geometry is not unbinding.
            if let form = changes["form"] as? String, payload != nil {
                guard ["dot","min","full"].contains(form) else { throw ReaderNativeNoteRules.NoteError.invalid("卡片形态") }
                payload?["form"] = form == "min" && binding(note) != nil ? "full" : form
                body["collapsed"] = false; changedPayload = true
            }
            if changedPayload, let name, let payload { body[name] = payload }
            if changes["w"] != nil || changes["h"] != nil {
                guard let w = ReaderNativeStrokeRules.numeric(changes["w"]), let h = ReaderNativeStrokeRules.numeric(changes["h"]),
                      w > 0, h > 0 else { throw ReaderNativeNoteRules.NoteError.invalid("卡片尺寸") }
                body["w"] = max(60,min(4000,w.rounded(.toNearestOrAwayFromZero)))
                body["h"] = max(40,min(4000,h.rounded(.toNearestOrAwayFromZero)))
            }
            guard body.count > 2 else { throw ReaderNativeNoteRules.NoteError.invalid("没有可保存的字段") }
        } else { throw ReaderNativeNoteRules.NoteError.invalid("未登记的卡片操作") }
        return Request(method:"PATCH",body:body)
    }

    private static func cardInk(_ input:[String:Any],note:[String:Any],now:Int64) throws -> (strokes:[[String:Any]],ratio:Double) {
        guard let action = input["kind"] as? String, ["commit","erase","createRegion"].contains(action),
              let opID = input["opId"] as? String, ReaderNativeStrokeRules.validID(opID),
              let segments = input["segments"] as? [[String:Any]], !segments.isEmpty, segments.count <= 64 else {
            throw ReaderNativeNoteRules.NoteError.invalid("卡片笔迹操作")
        }
        let existingRatio = ReaderNativeStrokeRules.numeric(note["iar"]) ?? 0
        let ratio = existingRatio != 0 ? existingRatio : ReaderNativeStrokeRules.numeric(input["aspectRatio"]) ?? 0
        guard ratio.isFinite, ratio > 0, ratio <= 100 else { throw ReaderNativeNoteRules.NoteError.invalid("画布比例") }
        var strokes = note["strokes"] as? [[String:Any]] ?? []
        for (index,segment) in segments.enumerated() {
            guard let raw = segment["points"] as? [[Any]], !raw.isEmpty, raw.count <= 4096 else { throw ReaderNativeNoteRules.NoteError.invalid("笔迹点数") }
            let points: [[Double]] = try raw.map { p in
                guard p.count == 2, let x = coordinate(p[0]), let y = coordinate(p[1]) else { throw ReaderNativeNoteRules.NoteError.invalid("笔迹坐标") }
                return [x,y]
            }
            if action == "erase" {
                for point in points { strokes.removeAll { ReaderNativeStrokeRules.hit($0,point:point,threshold:0.018) } }
            } else {
                guard points.count >= (action == "createRegion" ? 3 : 2) else { throw ReaderNativeNoteRules.NoteError.invalid("笔迹点数不足") }
                let nativeID = opID + ":" + String(index)
                if strokes.contains(where: { $0["nativeOpId"] as? String == nativeID }) { continue }
                let rawColor = segment["color"] as? String ?? ""
                let color = rawColor.range(of:"^#[a-fA-F0-9]{6}$",options:.regularExpression) != nil ? rawColor : "#ff3b30"
                let w = ReaderNativeStrokeRules.numeric(segment["width"]) ?? 0
                let width = max(0.3,min(48,w == 0 ? 4 : w))
                var stroke: [String:Any] = ["c":color,"w":width,"pts":points,"nativeOpId":nativeID]
                if let widths = segment["widths"] as? [Any], widths.count == points.count {
                    stroke["ww"] = widths.map { value in
                        let w = ReaderNativeStrokeRules.numeric(value) ?? 0
                        return max(0.3,min(48,w == 0 ? width : w))
                    }
                }
                if action == "createRegion" { stroke["t"] = "region"; stroke["id"] = opID + "-" + String(index); stroke["createdAtEpochMs"] = now }
                strokes.append(stroke)
            }
        }
        ReaderNativeStrokeRules.ensureRegionOrdinals(&strokes)
        return (strokes,ratio)
    }
    private static func integer(_ value:Any?) -> Int? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite,
              n.doubleValue.rounded() == n.doubleValue, abs(n.doubleValue) < 9_007_199_254_740_991 else { return nil }
        return n.intValue
    }
    private static func coordinate(_ value:Any?) -> Double? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
              n.doubleValue.isFinite, n.doubleValue >= 0, n.doubleValue <= 1 else { return nil }
        return n.doubleValue
    }
    private static func bytes(_ value:Any) throws -> Data {
        try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys,.fragmentsAllowed,.withoutEscapingSlashes])
    }
}
