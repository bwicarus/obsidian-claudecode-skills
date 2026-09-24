import Foundation

/// Display data comes from original events. The compatibility adapter carries
/// only scoped action handles; it must not render or truncate card content to
/// discover what the native sidebar should show.
enum ReaderNativeConversationProjection {
    static func part(_ value: [String: Any]) -> [String: Any] {
        var result = value
        var data = value["data"] as? [String: Any] ?? [:]
        guard let detail = data["nativeDetail"] as? [String: Any],
              let original = detail["content"] as? [String: Any] else { return value }
        let kind = detail["kind"] as? String ?? "artifact"
        func string(_ source: [String: Any], _ key: String) -> String { source[key] as? String ?? "" }
        func first(_ values: String...) -> String { values.first(where: { !$0.isEmpty }) ?? "" }
        func fields(_ source: [String: Any], _ keys: [String]) {
            for key in keys {
                if let text = source[key] as? String { data[key] = text }
                else if let number = source[key] as? NSNumber, number.doubleValue.isFinite { data[key] = number }
            }
        }
        result["title"] = first(string(detail, "title"), string(original, "title"), "生成物")
        result["text"] = ""
        result["status"] = "saved"
        switch kind {
        case "tool":
            result["kind"] = "tool"
            let status = toolStatus(original)
            result["status"] = status
            result["title"] = first(string(original, "label"), string(original, "tool"), "工具调用")
            result["text"] = ["failed": "操作失败", "running": "处理中", "completed": "已完成"][status] ?? "查看处理详情"
            let supplied = original["steps"] as? [[String: Any]] ?? []
            let steps = supplied.isEmpty ? [original] : supplied
            data["tool"] = string(original, "tool")
            data["stepCount"] = steps.count
            for (key, state) in [("successCount", "completed"), ("failureCount", "failed"), ("runningCount", "running")] {
                data[key] = steps.filter { toolStatus($0) == state }.count
            }
        case "anki":
            result["kind"] = "anki"
            // A committed group projection outranks the historical event. Keep
            // the original slot number, including holes left by removed cards.
            let live = data["nativeCard"] as? [String: Any]
            let card = live?["card"] as? [String: Any] ?? original["card"] as? [String: Any] ?? [:]
            result["title"] = first(string(card, "title"), "学习卡片")
            result["status"] = data["draft"] as? Bool == true ? "draft" : "saved"
            fields(card, ["front", "back", "question", "answer", "type", "cloze", "text", "explanation"])
            data["front"] = card["front"] ?? card["question"] ?? card["cloze"] ?? card["text"] ?? ""
            data["back"] = card["back"] ?? card["answer"] ?? ""
        case "fact", "general", "weather", "news":
            result["kind"] = kind
            let content = original["data"] as? [String: Any] ?? [:]
            fields(content, ["answer", "detail", "text", "summary", "description", "loc", "date", "lo", "hi", "cond", "precip", "tip"])
            if kind == "news", let items = content["items"] as? [[String: Any]] {
                data["items"] = items.map { $0.filter { ["t", "s", "src"].contains($0.key) } }
            }
            if kind == "general", string(data, "text").isEmpty { data["text"] = string(original, "brief") }
            // Only the short subtitle is bounded. Native inspection, copying,
            // Markdown rendering and drag/drop always retain the full original.
            result["text"] = String(first(string(content, "answer"), string(content, "text"), string(original, "brief")).prefix(1600))
        case "images", "videos":
            result["kind"] = kind
            // Routes, original indices, maps and video identities are projected
            // by ReaderNativeMediaArtifact, after these action handles merge.
        default:
            result["text"] = String(string(original, "brief").prefix(1600))
        }
        result["data"] = data
        return result
    }

    private static func toolStatus(_ value: [String: Any]) -> String {
        let status = value["status"] as? String ?? ""
        let error = value["error"]
        let hasError: Bool
        if error == nil || error is NSNull { hasError = false }
        else if let text = error as? String { hasError = !text.isEmpty }
        else if let number = error as? NSNumber { hasError = number.doubleValue != 0 }
        else { hasError = true }
        if hasError || ["error", "failed", "failure"].contains(status) { return "failed" }
        if ["running", "started", "in_progress", "pending"].contains(status) { return "running" }
        if ["done", "completed", "success", "succeeded"].contains(status) || (value["result"] != nil && !(value["result"] is NSNull)) { return "completed" }
        return "unknown"
    }
}
