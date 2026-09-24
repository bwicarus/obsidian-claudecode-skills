import Foundation

/// Native display policy for draft, review and saved cards. Inputs are data;
/// no HTML document, hidden controls, layout or JavaScript renderer is needed.
enum ReaderNativeCardPresentation {
    /// A page placement carries the whole learning group, including removed
    /// slots. Compacting it would change cardIndex and detach review receipts.
    static func placementCards(_ record: [String: Any]) -> [[String: Any]]? {
        guard record["deleted"] as? Bool != true, let gid = record["gid"] as? String,
              let cards = record["cards"] as? [[String: Any]], !cards.isEmpty,
              let states = record["states"] as? [String: Any] else { return nil }
        var result: [[String: Any]] = []
        for (index, original) in cards.enumerated() {
            guard let state = states[String(index)] as? [String: Any] else { return nil }
            var card = original
            card["type"] = card["type"] ?? "basic"
            card["front"] = card["front"] ?? card["question"] ?? ""
            card["back"] = card["back"] ?? card["answer"] ?? ""
            card["cloze"] = card["cloze"] ?? card["text"] ?? ""
            card["_st"] = card["_st"] ?? "draft"
            card["_showBack"] = card["_showBack"] as? Bool ?? false
            card["_nid"] = card["_nid"] ?? card["note_id"] ?? NSNull()
            card["_next"] = card["_next"] ?? NSNull()
            // Reuse the canonical presentation policy; a zero revision forces
            // state/projection application without reading any hidden card UI.
            var liveRecord = record
            var liveStates = states
            var liveState = state; liveState["removed"] = false
            liveStates[String(index)] = liveState; liveRecord["states"] = liveStates
            let input: [String: Any] = ["gid": gid, "cardIndex": index, "card": card,
                                        "entityRev": 0, "stateRev": 0]
            guard let data = applying(liveRecord, to: ["nativeCard": input]),
                  let updated = data["nativeCard"] as? [String: Any],
                  var value = updated["card"] as? [String: Any] else { return nil }
            value["_removed"] = state["removed"] as? Bool ?? false
            result.append(value)
        }
        return result
    }

    /// Apply an authoritative local receipt immediately. Late web snapshots
    /// cannot roll an edit back or make the next field save use an old revision.
    static func applying(_ record: [String: Any], to data: [String: Any]) -> [String: Any]? {
        guard var input = data["nativeCard"] as? [String: Any], let gid = input["gid"] as? String,
              gid == record["gid"] as? String, let index = input["cardIndex"] as? Int,
              let entityRev = record["entityRev"] as? NSNumber, let stateRev = record["stateRev"] as? NSNumber else { return data }
        let priorEntity = (input["entityRev"] as? NSNumber)?.int64Value ?? 0
        let priorState = (input["stateRev"] as? NSNumber)?.int64Value ?? 0
        guard entityRev.int64Value >= priorEntity, stateRev.int64Value >= priorState,
              entityRev.int64Value > priorEntity || stateRev.int64Value > priorState else { return data }
        if record["deleted"] as? Bool == true { return nil }
        guard let cards = record["cards"] as? [[String: Any]], cards.indices.contains(index),
              let saved = (record["states"] as? [String: Any])?[String(index)] as? [String: Any],
              var card = input["card"] as? [String: Any] else { return data }
        if saved["removed"] as? Bool == true { return nil }
        card.merge(cards[index]) { _, next in next }
        let stateFields: Set<String> = ["front", "back", "cloze", "_st", "_nid", "_next", "_showBack", "id", "card_id",
            "_ratingUnavailable", "_ratingUnavailableReason", "_ratingPending", "_syncPending", "_ratingAid", "_ratingEase",
            "_ratingCardId", "_addPending", "_addQueued", "_addAid", "_pcExportAid", "_pcExportStatus", "_mobileExportStatus"]
        card.merge((saved["exactState"] as? [String: Any] ?? [:]).filter { stateFields.contains($0.key) }) { _, next in next }
        if saved["phase"] as? String == "confirmed" {
            if card["_st"] as? String == "draft" {
                card["_st"] = "learn"; card["_ratingUnavailable"] = true; card["_ratingUnavailableReason"] = "not-exported"
            }
            if card["_addPending"] as? Bool == true {
                card["_addPending"] = false; card["_addQueued"] = false; card["_addAid"] = NSNull()
            }
        }
        let projections = (saved["projections"] as? [String: Any])?["anki"] as? [String: Any] ?? [:]
        var statuses = Set<String>()
        for (target, value) in projections {
            guard target == "readerpc" || target.hasPrefix("ankimobile"), let receipt = value as? [String: Any],
                  let status = receipt["status"] as? String else { continue }
            card[target == "readerpc" ? "_pcExportStatus" : "_mobileExportStatus"] = status; statuses.insert(status)
        }
        for (status, reason) in [("unknown", "export-unknown"), ("pending", "export-pending"), ("succeeded", "external"), ("failed", "not-exported")] {
            guard statuses.contains(status) else { continue }
            card["_ratingUnavailable"] = true
            if status != "failed" || saved["phase"] as? String == "confirmed" { card["_ratingUnavailableReason"] = reason }
            break
        }
        input["card"] = card; input["entityRev"] = entityRev; input["stateRev"] = stateRev
        var next = data; next["nativeCard"] = input
        return project(next)
    }

    static func interaction(_ input: [String: Any]) -> [String: Any]? {
        guard let c = input["card"] as? [String: Any], c["_removed"] as? Bool != true,
              let index = input["cardIndex"] as? Int, index >= 0 else { return nil }
        func flag(_ key: String) -> Bool { c[key] as? Bool == true }
        func string(_ key: String) -> String { c[key] as? String ?? "" }
        let state = string("_st"), readonly = input["readonly"] as? Bool == true
        let pending = ["_addPending", "_removePending", "_ratingPending", "_syncPending"].contains(where: flag)
        var controls: [[String: Any]] = [], fields: [[String: Any]] = []
        func control(_ key: String, _ title: String, destructive: Bool = false) {
            controls.append(["key": key, "title": title, "destructive": destructive, "disabled": pending || readonly])
        }
        if state == "draft" {
            fields = (string("type") == "cloze" ? ["cloze"] : ["front", "back"]).map { ["key": $0, "value": string($0)] }
            control("del", "删除", destructive: true); control("add", "保存到 Reader 卡库")
        } else if state != "done", state != "preview", !flag("_addPending") {
            if !flag("_showBack") { control("reveal", "显示答案") }
            else if !flag("_ratingUnavailable") {
                for (index, label) in ["再来", "困难", "良好", "简单"].enumerated() { control("rate-\(index + 1)", label) }
            } else if string("_ratingUnavailableReason") == "not-exported" {
                if input["canDesktop"] as? Bool == true, string("_pcExportStatus") == "failed" { control("export-desktop", "重发到电脑 Anki") }
                if input["canMobile"] as? Bool == true { control("export-mobile", "同步到 iPad Anki") }
            }
        }
        func face(_ side: String) -> [String: Any] {
            let display = c[side == "front" ? "_displayFrontHtml" : "_displayBackHtml"]
            let custom = display != nil && !(display is NSNull)
            var raw = custom ? String(describing: display!) : string(string("type") == "cloze" ? "cloze" : side)
            if !custom, string("type") == "cloze", let pattern = try? NSRegularExpression(pattern: #"\{\{c\d+::(.*?)(::[^}]*)?\}\}"#) {
                raw = pattern.stringByReplacingMatches(in: raw, range: NSRange(raw.startIndex..., in: raw), withTemplate: side == "back" ? "**$1**" : "**[…]**")
            }
            let html = custom || raw.range(of: #"<[a-z][\s\S]*>"#, options: [.regularExpression, .caseInsensitive]) != nil
            return ["side": side, "format": html ? "html" : "markdown", "content": raw]
        }
        let showBack = state == "done" || state == "preview" || (flag("_showBack") && !flag("_addPending"))
        var faces = input["controlledReview"] as? Bool == true && string("_revealMode") == "replace" && showBack ? [] : [face("front")]
        if showBack { faces.append(face("back")) }
        var notice = ""
        if flag("_addPending") { notice = flag("_addQueued") ? "保存结果待核对，请勿重复提交" : "正在保存到 Reader 卡库" }
        else if flag("_syncPending") { notice = "评分待同步" }
        else if flag("_ratingPending") { notice = "正在提交评分" }
        else if state == "done" {
            let interval = (c["_next"] as? [String: Any])?["interval"] as? NSNumber
            let value = interval?.doubleValue ?? 0
            let next: String
            if value.isFinite && value > 0 {
                next = value >= 1 ? "\(interval!.stringValue) 天后" : "\(max(1, Int(floor(value * 24 * 60 + 0.5)))) 分钟后"
            } else { next = "很快" }
            notice = "已复习 · 距下次复习 " + next
        } else if showBack && flag("_ratingUnavailable") {
            notice = ["external": "已发送到外部 Anki；请在对应 Anki 中复习。",
                "export-pending": "已打开外部 Anki，正在等待回到 Reader 的确认。",
                "export-unknown": "外部 Anki 接收结果未知，已阻止重复发送。",
                "missing": "暂未取得唯一 Anki 卡号，请在 Anki 中复习。",
                "multiple": "这条 Anki 笔记生成了多张卡，请在 Anki 中选择具体卡片复习。"][string("_ratingUnavailableReason")] ?? ""
        }
        return ["gid": input["gid"] as? String ?? "", "cardIndex": index, "state": state, "pending": pending,
                "editable": state == "draft" && !pending && !readonly, "fields": fields, "controls": controls,
                "presentation": ["faces": faces, "notice": notice]]
    }

    static func project(_ data: [String: Any]) -> [String: Any] {
        guard let input = data["nativeCard"] as? [String: Any], let state = interaction(input),
              let presentation = state["presentation"] as? [String: Any] else { return data }
        var result = data
        var ids = data["nativeCardActions"] as? [String:String] ?? [:]
        if let owner = data["nativeActionOwner"] as? String, !owner.isEmpty {
            for key in ["del","add","reveal","rate-1","rate-2","rate-3","rate-4","export-desktop","export-mobile","edit-front","edit-back","edit-cloze"] {
                ids[key] = "native-card:" + owner + ":" + key
            }
            result["nativeCardActions"] = ids
        }
        guard !ids.isEmpty else { return data }
        for key in ["state", "editable", "pending"] { result[key] = state[key] }
        result["live"] = true
        result["faces"] = presentation["faces"]; result["notice"] = presentation["notice"]
        result["controls"] = (state["controls"] as? [[String: Any]] ?? []).compactMap { control -> [String: Any]? in
            guard let key = control["key"] as? String, let id = ids[key] else { return nil }
            var item = control; item["id"] = id; return item
        }
        result["fields"] = (state["fields"] as? [[String: Any]] ?? []).compactMap { field -> [String: Any]? in
            guard let key = field["key"] as? String, let id = ids["edit-" + key] else { return nil }
            var item = field; item["id"] = id; return item
        }
        return result
    }
}
