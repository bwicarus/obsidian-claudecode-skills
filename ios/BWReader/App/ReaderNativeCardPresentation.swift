import Foundation

/// Native display policy for draft, review and saved cards. Inputs are data;
/// no HTML document, hidden controls, layout or JavaScript renderer is needed.
enum ReaderNativeCardPresentation {
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
              let ids = data["nativeCardActions"] as? [String: String], let presentation = state["presentation"] as? [String: Any] else { return data }
        var result = data
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
