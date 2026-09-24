import Foundation

/// Semantic state for one streamed answer. Web compatibility handlers receive
/// effects and a read projection; they no longer decide answer ownership or
/// reparse each native text increment for voice/display/follow-up markers.
struct ReaderNativeAssistantTurn {
    private(set) var answer = ""
    private(set) var sawTool = false
    private(set) var sawCLICard = false
    private(set) var done = false

    mutating func consume(_ event: ReaderNativeAssistantEvent) throws -> [String: Any] {
        guard !done else { throw ReaderNativeAssistantStream.Failure("对话已结束，拒绝晚到事件") }
        let value = (try? JSONSerialization.jsonObject(with: Data(event.data.utf8), options: [.fragmentsAllowed])) ?? event.data
        var textChanged = false
        switch event.name {
        case "answer":
            guard let text = value as? String else { throw ReaderNativeAssistantStream.Failure("回答内容不是文本") }
            answer = text; textChanged = true
        case "error":
            answer = "⚠️ " + Self.scalarText(value); textChanged = true
        case "tool2":
            if let tool = value as? [String: Any], let name = tool["name"] as? String, !name.isEmpty {
                if Self.truthy(tool["task_id"]), ["do_task", "make_paper", "read_check_report", "run_saved_task"].contains(name) {
                    sawCLICard = true
                } else { sawTool = true }
            }
        case "done": done = true
        default: break
        }
        var projection: [String: Any] = ["sawTool": sawTool, "sawCliCard": sawCLICard, "done": done]
        if textChanged {
            let content = Self.content(answer)
            projection["answer"] = answer
            projection["voiceText"] = content.voiceText
            projection["displayText"] = content.displayText
            projection["finalDisplayText"] = content.finalDisplayText
            projection["followups"] = content.followups
        }
        return projection
    }

    struct Content {
        let voiceText: String
        let displayText: String
        let finalDisplayText: String
        let followups: [String]
    }

    static func content(_ answer: String) -> Content {
        var followups: [String] = []
        func append(_ body: String) {
            for question in body.components(separatedBy: CharacterSet(charactersIn: "|\n")) {
                let value = replacing(question.trimmingCharacters(in: .whitespacesAndNewlines), #"^[\-·•0-9\.\s]+"#, "")
                if !value.isEmpty, followups.count < 4 { followups.append(value) }
            }
        }
        let expression = try! NSRegularExpression(pattern: #"\[\[FOLLOWUP\]\]([\s\S]*?)\[\[/FOLLOWUP\]\]"#)
        let text = answer as NSString
        var clean = "", cursor = 0
        for match in expression.matches(in: answer, range: NSRange(location: 0, length: text.length)) {
            clean += text.substring(with: NSRange(location: cursor, length: match.range.location - cursor))
            append(text.substring(with: match.range(at: 1)))
            cursor = NSMaxRange(match.range)
        }
        clean += text.substring(from: cursor)
        if let open = clean.range(of: "[[FOLLOWUP]]") {
            append(replacing(String(clean[open.upperBound...]), #"\[\[/?FOLLOWUP\]\]"#, ""))
            clean = String(clean[..<open.lowerBound])
        }
        clean = clean.trimmingCharacters(in: .whitespacesAndNewlines)
        var voice = clean
        let tag = "[[FOLLOWUP]]"
        for count in stride(from: tag.count - 1, through: 2, by: -1) {
            let prefix = String(tag.prefix(count))
            if voice.hasSuffix(prefix) { voice.removeLast(count); break }
        }
        return .init(voiceText: voice, displayText: stripMood(voice), finalDisplayText: stripMood(clean), followups: followups)
    }

    private static func stripMood(_ value: String) -> String {
        let complete = replacing(value, #"[\[【]语气[::]\s*([^\]】]{1,12})[\]】]\s*"#, "")
        return replacing(complete, #"[\[【]语气?[::]?[^\]】]{0,12}$"#, "")
    }

    private static func replacing(_ value: String, _ pattern: String, _ replacement: String) -> String {
        let expression = try! NSRegularExpression(pattern: pattern)
        return expression.stringByReplacingMatches(in: value, range: NSRange(location: 0, length: (value as NSString).length), withTemplate: replacement)
    }

    private static func truthy(_ value: Any?) -> Bool {
        if value == nil || value is NSNull { return false }
        if let text = value as? String { return !text.isEmpty }
        if let number = value as? NSNumber { return number.doubleValue != 0 }
        return true
    }

    private static func scalarText(_ value: Any) -> String {
        if let value = value as? String { return value }
        if value is NSNull { return "null" }
        if let value = value as? NSNumber { return value.stringValue }
        return "[object Object]"
    }
}
