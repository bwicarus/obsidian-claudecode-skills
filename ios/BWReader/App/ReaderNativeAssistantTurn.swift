import Foundation

/// Only book mutation events need the document commit adapter. Text, tool
/// telemetry and completion stay in the native stream; receipts replace their
/// original slots without changing the SSE cursor or event ordering.
struct ReaderNativeAssistantEventBatch {
    let events: [ReaderNativeAssistantEvent]
    let actions: [ReaderNativeAssistantEvent]
    init(_ events: [ReaderNativeAssistantEvent]) throws {
        guard events.count <= 10000 else { throw ReaderNativeAssistantStream.Failure("对话事件批次过大") }
        self.events = events
        actions = events.filter { $0.name == "actions" }
        for event in actions {
            guard (try? JSONSerialization.jsonObject(with: Data(event.data.utf8))) is [Any] else {
                throw ReaderNativeAssistantStream.Failure("助手书籍动作列表无效，未执行本批改动")
            }
        }
    }
    func committed(_ receipts: [ReaderNativeAssistantEvent]) throws -> [ReaderNativeAssistantEvent] {
        guard receipts.count == actions.count,
              receipts.allSatisfy({ $0.name == "actions" && (try? JSONSerialization.jsonObject(with: Data($0.data.utf8))) is [Any] }) else {
            throw ReaderNativeAssistantStream.Failure("助手书籍改动回执不完整，未重复执行")
        }
        var index = 0
        return events.map { event in
            guard event.name == "actions" else { return event }
            defer { index += 1 }
            return receipts[index]
        }
    }
}

/// Semantic state for one streamed answer. Web compatibility handlers receive
/// effects and a read projection; they no longer decide answer ownership or
/// reparse each native text increment for voice/display/follow-up markers.
struct ReaderNativeAssistantTurn {
    private(set) var answer = ""
    private(set) var sawTool = false
    private(set) var sawCLICard = false
    private(set) var done = false
    private(set) var trace: [Any] = []
    private(set) var recoveredAt: Double = 0

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
        case "trace": trace = value as? [Any] ?? []
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

    mutating func restore(_ message: [String: Any]) throws {
        guard answer.isEmpty, let text = message["content"] as? String, !text.isEmpty else {
            throw ReaderNativeAssistantStream.Failure("恢复回答与当前轮次不符")
        }
        answer = text
        if let value = message["trace"] as? [Any] { trace = value }
        recoveredAt = (message["ts"] as? NSNumber)?.doubleValue ?? 0
    }

    /// Final ownership and display decisions are made once in Swift. The
    /// compatibility producer observes this result; it does not choose a
    /// second answer, reparse follow-ups or run a separate recovery loop.
    func completion(aborted: Bool, error: String? = nil) -> [String: Any] {
        let raw = error.map { "⚠️ " + $0 } ?? answer
        let content = Self.content(raw)
        return ["answer": raw, "voiceText": content.voiceText,
                "displayText": content.finalDisplayText, "finalDisplayText": content.finalDisplayText,
                "followups": aborted ? [] : content.followups,
                "sawTool": sawTool, "sawCliCard": sawCLICard, "done": true,
                "target": sawCLICard ? "task" : (sawTool ? "turn" : "answer"),
                "aborted": aborted, "trace": trace, "recoveredAt": recoveredAt,
                "statusText": aborted ? "已停止" : "没拿到回答(可以重问一次)"]
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
