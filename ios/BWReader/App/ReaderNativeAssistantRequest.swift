import Foundation

/// Freeze one outgoing turn. View code supplies the current reader snapshot;
/// native code owns prompt defaults, book-context policy and request identity.
struct ReaderNativeAssistantRequest {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    let body: [String: Any]

    init(_ input: [String: Any], identity: () -> String = { UUID().uuidString.lowercased() }) throws {
        guard let original = input["message"] as? String, original.utf16.count <= 32000,
              let mode = input["assistant_mode"] as? String, ["normal", "review"].contains(mode),
              var context = input["context"] as? [String: Any], JSONSerialization.isValidJSONObject(context) else {
            throw Failure(message: "对话内容或上下文无效")
        }
        if input["no_book"] as? Bool == true || context["no_book"] as? Bool == true {
            context["no_book"] = true
            for key in ["current_section_idx", "section", "selection_sentence", "selection_anchor", "visible_text"] {
                context.removeValue(forKey: key)
            }
            context["page"] = 0
        }
        // Explicit pins, figures and selected text survive disabling the book.
        var message = original.trimmingCharacters(in: .whitespacesAndNewlines)
        if message.isEmpty {
            let figures = context["figures"] as? [[String: Any]] ?? []
            let notes = context["notes"] as? [Any] ?? []
            let focus = context["focus_sel"] as? [String: Any] ?? [:]
            if !figures.isEmpty { message = figures.allSatisfy { $0["kind"] as? String == "note" } ? "讲讲这个便签" : "讲讲这张图" }
            else if !notes.isEmpty { message = "讲讲这个便签" }
            else if !(focus["text"] as? String ?? "").isEmpty { message = focus["kind"] as? String == "formula" ? "讲讲这个公式" : "讲讲这段" }
            else if !(context["selection"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { message = "讲讲这段" }
            else { throw Failure(message: "消息和选中内容均为空") }
        }
        let key = identity()
        guard !key.isEmpty, key.utf8.count <= 100,
              key.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95 }) else {
            throw Failure(message: "对话编号无效")
        }
        var result: [String: Any] = ["message": message, "context": context, "assistant_mode": mode,
                                     "rid": "c" + key, "turn_id": "t" + key]
        for option in ["media_prefer", "force_effort", "force_model"] {
            if let value = input[option], !(value is NSNull) { result[option] = value }
        }
        if mode == "normal", let voice = input["voice"] as? NSNumber, voice.intValue == 1 { result["voice"] = 1 }
        guard JSONSerialization.isValidJSONObject(result),
              try JSONSerialization.data(withJSONObject: result).count <= 8 * 1024 * 1024 else {
            throw Failure(message: "对话上下文过大")
        }
        body = result
    }
}
