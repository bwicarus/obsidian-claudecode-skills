import Foundation
import CryptoKit
import SwiftSoup

/// Projection of stored tool/HTML cards. It consumes note data, never DOM
/// nodes, a web snapshot, or a second set of page coordinates.
enum ReaderNativeHTMLNotes {
    struct Action { let noteID:String; let key:String; var resource:String? = nil }
    struct Snapshot { var placements:[[String:Any]] = []; var actions:[String:Action] = [:] }
    static func project(_ notes:[[String:Any]],bookID:String,page:Int,pinned:Set<String>) throws -> Snapshot {
        var result = Snapshot()
        for note in notes {
            guard ReaderNativeNoteActions.slot(note) == "html", let html = note["html"] as? [String:Any],
                  let id = note["id"] as? String, let anchor = note["anchor"] as? [String:Any],
                  anchor["kind"] as? String == "pdf" else { continue }
            let bind = ReaderNativeNoteActions.binding(note)
            let anchorPage = (anchor["page"] as? NSNumber)?.intValue ?? 0
            let bindPage = (bind?["page"] as? NSNumber)?.intValue ?? 0
            guard abs(anchorPage-page) <= 4 || (bindPage > 0 && abs(bindPage-page) <= 4) else { continue }
            let uid = "native-note-" + SHA256.hash(data:Data((bookID + "\n" + id).utf8)).prefix(16).map { String(format:"%02x",$0) }.joined()
            func action(_ key:String,resource:String? = nil) -> String {
                let token = uid + ":" + key
                result.actions[token] = Action(noteID:id,key:key,resource:resource)
                return token
            }
            let label = html["label"] as? String ?? "学习卡"
            let content = html["content"] as? String ?? ""
            let cid = html["cid"] as? String ?? id
            var controls: [String:String] = [:]
            for key in ["move","anchor","form","collapse","expand","resize","remove","trash","favorite","ink"] { controls[key] = action(key) }
            var images: [String:String] = [:]
            if content.range(of:"<img\\b",options:[.regularExpression,.caseInsensitive]) != nil {
                let document = try SwiftSoup.parseBodyFragment(content)
                for (index,element) in try document.select("img[src]").array().prefix(64).enumerated() {
                    let source = try element.attr("src")
                    if let route = mediaRoute(source) { images[source] = action("image-" + String(index),resource:route) }
                }
            }
            let part: [String:Any] = ["id":uid + "-html","kind":"general","title":label,"status":"saved","actionId":action("inspect"),
                "data":["text":content,"format":html["isHtml"] as? Bool == true ? "html":"text",
                    "selectId":action("select"),"pinId":action("pin"),"dragId":controls["move"]!,"pinned":pinned.contains(cid),"inlineImages":images]]
            let form = html["form"] as? String ?? (note["collapsed"] as? Bool == true ? "dot":"full")
            let geometry = String(decoding:try JSONSerialization.data(withJSONObject:ReaderNativeNoteActions.geometry(note),options:[.sortedKeys,.withoutEscapingSlashes]),as:UTF8.self)
            result.placements.append(["id":uid,"noteId":id,"source":"note","title":label,"bound":bind != nil,"pinned":bind != nil,
                "collapsed":form != "full","visible":true,"open":false,"form":form,"tone":tone(html,bound:bind != nil),
                "controls":controls,"parts":[part],"markers":[],"size":NSNull(),
                "rect":["x":0,"y":0,"width":0,"height":0],
                "ink":["strokes":note["strokes"] ?? [],"aspectRatio":note["iar"] ?? 0,"geometry":geometry]])
        }
        return result
    }
    static func mediaRoute(_ source: String) -> String? { ReaderNativeMediaRoute.route(source) }
    private static func tone(_ html:[String:Any],bound:Bool) -> String {
        let old = html["type"] as? String ?? ""
        guard bound else { return old }
        let text = ((html["category"] as? String ?? html["kind"] as? String ?? "").lowercased()) + " " + (html["label"] as? String ?? "")
        for (pattern,color) in [("image|images|video|配图|图片|图像|视频","#34d399"),("number|numeric|metric|weather|数值|数字|数据|统计|温度|价格","#ff9f0a"),
            ("qa|question|anki|quiz|问答|考点|出题|题目|学习卡","#7dd3fc"),("text|文字|背景|辨析|摘要|翻译|解释|新闻","#bf5af2")] {
            if text.range(of:pattern,options:.regularExpression) != nil { return color }
        }
        if ["#c77dff","#34d399","#ff7a59"].contains(old.lowercased()) { return "#34d399" }
        if ["#39d98a","#7dd3fc"].contains(old.lowercased()) { return "#7dd3fc" }
        if ["#2dd4bf","#ff9f0a"].contains(old.lowercased()) { return "#ff9f0a" }
        return "#bf5af2"
    }
}
