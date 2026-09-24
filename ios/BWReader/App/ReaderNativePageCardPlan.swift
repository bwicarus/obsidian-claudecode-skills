import Foundation

/// Document/card validation and stable replay identity; rendering is never used
/// to decide whether an assistant may change stored content.
enum ReaderNativePageCardPlan {
    typealias O = [String:Any]
    typealias F = ReaderNativeAssistantEdits.Failure
    typealias HTML = (String) throws -> (content:String, text:String)
    static func json(_ value: Any) throws -> String { String(decoding:try ReaderNativeCardRules.bytes(value),as:UTF8.self) }
    static func fields(_ value: O, _ keys: Set<String>) throws {
        guard Set(value.keys).isSubset(of:keys) else { throw F("页面卡片包含不支持的修改字段") }
    }
    static func text(_ value: Any?, limit: Int = 100_000) throws -> String {
        guard let value = value as? String, value.utf8.count <= limit else { throw F("页面卡片文字无效或过长") }; return value
    }
    static func integer(_ value: Any?, min: Int64 = 0, max: Int64 = 9_007_199_254_740_991) throws -> Int64 {
        let n = try ReaderNativeCardRules.integer(value,"页面卡片数值")
        guard n >= min, n <= max else { throw F("页面卡片数值超出范围") }; return n
    }
    static func plain(_ text: String) -> String {
        var value = text.replacingOccurrences(of:"<(button|script|style|noscript|template)\\b[^>]*>[\\s\\S]*?</\\1\\s*>",with:" ",options:[.regularExpression,.caseInsensitive])
        value = value.replacingOccurrences(of:"<[^>]+>",with:" ",options:.regularExpression)
        for (pattern, replacement) in [("&nbsp;|&#160;"," "),("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;","\""),("&#39;|&apos;","'")] {
            value = value.replacingOccurrences(of:pattern,with:replacement,options:[.regularExpression,.caseInsensitive])
        }
        value = value.replacingOccurrences(of:"[\\s\\uFEFF]+",with:" ",options:.regularExpression).trimmingCharacters(in:.whitespacesAndNewlines)
        return String(decoding:Array(value.utf16.prefix(100_000)),as:UTF16.self)
    }
    static func cards(_ raw: Any?) throws -> [O] {
        guard let values = raw as? [O], (1...12).contains(values.count) else { throw F("学习卡替换需要 1 到 12 张卡片") }
        return try values.map { item in
            if item["type"] as? String == "basic", Set(item.keys) == ["type","front","back"] {
                let front = try text(item["front"]), back = try text(item["back"])
                guard !front.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty, !back.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { throw F("卡片正反面不能为空") }
                return ["type":"basic","front":front,"back":back]
            }
            if item["type"] as? String == "cloze", Set(item.keys) == ["type","cloze"] {
                let cloze = try text(item["cloze"])
                guard cloze.range(of:"\\{\\{c[1-9][0-9]*::[\\s\\S]+?\\}\\}",options:.regularExpression) != nil else { throw F("挖空卡缺少有效挖空") }
                return ["type":"cloze","cloze":cloze]
            }
            throw F("学习卡仅能替换 basic/cloze 内容")
        }
    }
    static func requestFingerprint(operation: String, id: String, revision: Int64, number: Any?, replacement: Any?) throws -> String {
        try json(["operation":operation,"expectedId":id,"expectedRevision":revision,"numberSpecified":number != nil && !(number is NSNull),
                  "number":number ?? NSNull(),"replacement":replacement ?? NSNull()])
    }
    static func action(_ data: O, authority: O, file: String, now: Int64, sanitize: HTML) throws -> O {
        try fields(data,["type","op","native_operation_id","file","page","number","expected_id","expected_revision","item"])
        guard data["type"] as? String == "page-card", let op = data["op"] as? String, ["edit","delete"].contains(op),
              data["file"] as? String == file, let id = data["expected_id"] as? String, let operationID = data["native_operation_id"] as? String,
              operationID.range(of:"^(npdf|pcard)_[0-9a-f]{24}$",options:.regularExpression) != nil,
              let item = data["item"] as? O, item["id"] as? String == id else { throw F("页面卡片动作身份无效") }
        try fields(item,op == "edit" ? ["id","before","after"] : ["id","before"])
        let page = try integer(data["page"],min:1,max:10_000_000), revision = try integer(data["expected_revision"])
        let number: Any = data["number"] == nil || data["number"] is NSNull ? NSNull() : try integer(data["number"],min:1,max:1_000_000)
        guard let rawBefore = item["before"] as? O else { throw F("页面卡片原始快照缺失") }
        let before = try ReaderNativeAssistantEdits.note(rawBefore,fallbackID:id,file:file,now:now)
        let slot = before["card"] is O ? "card" : "html", content = before[slot] as? O
        guard before["id"] as? String == id, (before["card"] is O) != (before["html"] is O), !(before["video"] is O) else { throw F("页面卡片类型或身份无效") }
        if let bind = content?["bind"] as? O {
            guard bind["kind"] as? String == "page-chars", try integer(bind["page"],min:1) == page,
                  try integer(bind["to"],max:1_000_000) >= integer(bind["from"],max:1_000_000) else { throw F("卡片原文锚点不匹配",conflict:true) }
            _ = try text(bind["text"] ?? "",limit:200)
        } else {
            guard number is NSNull, let anchor = before["anchor"] as? O, anchor["kind"] as? String == "pdf", try integer(anchor["page"]) == page else { throw F("卡片页码不匹配",conflict:true) }
        }
        var canonicalID: Any = NSNull()
        if slot == "card", let content {
            var ids:[String] = []
            for key in ["id","cid","gid"] where content[key] != nil && !(content[key] is NSNull) {
                guard let candidate = content[key] as? String else { throw F("学习卡身份无效",conflict:true) }
                if !candidate.isEmpty { ids.append(candidate) }
            }
            if let first = ids.first {
                guard ids.allSatisfy({ $0 == first }) else { throw F("学习卡 id/cid/gid 不一致",conflict:true) }
                if first.range(of:"^card_[a-f0-9]{4,64}$",options:.regularExpression) != nil { canonicalID = first }
            }
        }
        var after: O?, replacement: Any = NSNull(), replacementCards: Any = NSNull()
        if op == "edit" {
            guard let raw = item["after"] as? O else { throw F("页面卡片替换快照缺失") }
            after = try ReaderNativeAssistantEdits.note(raw,fallbackID:id,file:file,now:now)
            guard var face = after?[slot] as? O else { throw F("页面卡片替换类型不一致") }
            if slot == "card" {
                let changed = try cards(face["cards"])
                face["cards"] = changed; replacementCards = changed; replacement = ["cards":changed]
                face["contextText"] = plain(changed.map { card in card["type"] as? String == "cloze" ? plain(card["cloze"] as! String) : [plain(card["front"] as! String),plain(card["back"] as! String)].filter { !$0.isEmpty }.joined(separator:" / ") }.joined(separator:"\n"))
            } else {
                let safe = try sanitize(text(face["content"])); guard !safe.content.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty else { throw F("卡片 HTML 净化后为空") }
                face["content"] = safe.content; face["contextText"] = plain(safe.text); replacement = ["content":safe.content]
            }
            after?[slot] = face
        }
        let kind = "page-card-" + op, stable = number is NSNull || authority["page_card_stable_id"] as? Bool == true
        let plan: O = ["id":id,"page":page,"number":number,"expectedRevision":revision,"before":before,"after":after as Any? ?? NSNull(),"replacementCards":replacementCards,"canonicalId":canonicalID]
        let fingerprint = try json(["id":operationID,"kind":kind,"placementId":id,"page":page,"number":number,"before":before,"after":after as Any? ?? NSNull()])
        return ["operation":"apply","operationId":operationID,"kind":kind,"plan":plan,"fingerprint":fingerprint,
            "requestFingerprint":try requestFingerprint(operation:op,id:id,revision:revision,number:stable ? nil : number,replacement:replacement),"expectedState":authority]
    }
}
