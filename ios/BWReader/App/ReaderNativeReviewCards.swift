import Foundation

/// Original card content and its repository revisions travel together. This is
/// a read projection; it does not create another card or change its batch index.
enum ReaderNativeReviewCards {
    typealias O = [String: Any]
    private typealias R = ReaderNativeCardRules

    private static func hash(_ text: String) -> String {
        var value: UInt32 = 2166136261
        for char in text.utf16 { value = (value ^ UInt32(char)) &* 16777619 }
        return String(value, radix: 16)
    }
    static func identity(_ card: O) -> O {
        var result: O = [:]
        for (to, from) in [("card_id", "id"), ("note_id", "note_id"), ("local_id", "local_id"),
            ("entity_id", "entity_id"), ("source_ref", "source_ref"), ("source_url", "source_url"), ("deck", "deck")] {
            result[to] = R.string(card[from])
        }
        result["entity_index"] = card["entity_index"] ?? NSNull()
        return result
    }
    static func stableID(_ card: O) -> String {
        func part(_ raw: Any?) -> String {
            let text = R.string(raw)
            return text.range(of: #"\A[A-Za-z0-9_-]{1,72}\z"#, options: .regularExpression) != nil ? text : hash(text)
        }
        if !R.string(card["id"]).isEmpty { return "anki_card_" + part(card["id"]) }
        if !R.string(card["note_id"]).isEmpty { return "anki_note_" + part(card["note_id"]) }
        if !R.string(card["entity_id"]).isEmpty {
            return R.string(card["entity_id"]) + (R.has(card["entity_index"]) ? "_i" + part(card["entity_index"]) : "")
        }
        if !R.string(card["local_id"]).isEmpty { return "anki_local_" + part(card["local_id"]) }
        let record = identity(card)
        let keys = ["card_id", "note_id", "local_id", "entity_id", "entity_index", "source_ref", "source_url", "deck"]
        // Preserve the legacy fallback's ordered JSON hash; real Anki IDs and
        // Reader entity+index identities take precedence over this fallback.
        let pairs = keys.map { key -> String in
            let encoded = (try? JSONSerialization.data(withJSONObject: record[key]!, options: [.fragmentsAllowed, .withoutEscapingSlashes])) ?? Data("null".utf8)
            return "\"" + key + "\":" + String(decoding: encoded, as: UTF8.self)
        }
        return "anki_legacy_" + hash("{" + pairs.joined(separator: ",") + "}")
    }
    static func assistant(_ card: O) -> O {
        var result = identity(card)
        func face(_ preferred: String, _ fallback: String) -> String {
            let value = R.string(card[preferred]); return value.isEmpty ? R.string(card[fallback]) : value
        }
        result["id"] = stableID(card)
        result["front"] = face("question", "front"); result["question"] = result["front"]
        result["back"] = face("answer", "back"); result["answer"] = result["back"]
        result["native_review_faces"] = true; result["face_format"] = card["_localReview"] is O ? "markdown" : "html"
        result["reveal_mode"] = "append"; result["anki_note_id"] = card["note_id"] ?? NSNull()
        result["review_kind"] = R.string(card["review_kind"])
        result["candidate_reasons"] = Array((card["candidate_reasons"] as? [Any] ?? []).prefix(6))
        return result
    }
    static func deleteKind(_ card: O?) -> String {
        guard let card else { return "" }
        if card["_localReview"] is O { return "reader-card" }
        if let id = try? R.integer(card["note_id"] ?? card["noteId"], "Anki note id"), id > 0 { return "anki-note" }
        return ""
    }

    static func local(_ entry: O) throws -> O {
        let record = try R.object(entry["record"], "review record")
        let card = try R.object(entry["card"], "review card")
        let state = try R.object(entry["state"], "review state")
        let id = try R.text(record["id"], "review entity", 240, required: true)
        let index = try R.integer(entry["cardIndex"], "review card index")
        let source = record["source"] as? O ?? [:]
        func face(_ back: Bool) -> String {
            guard card["type"] as? String == "cloze" else { return R.string(card[back ? "back" : "front"]) }
            let original = R.string(card["cloze"] ?? card["text"])
            guard let pattern = try? NSRegularExpression(pattern: #"\{\{c[0-9]+::([\s\S]*?)(?:::[\s\S]*?)?\}\}"#) else { return original }
            return pattern.stringByReplacingMatches(in: original, range: NSRange(location: 0, length: original.utf16.count),
                withTemplate: back ? "<b>$1</b>" : "<b>[…]</b>")
        }
        var value = card
        value["question"] = face(false); value["answer"] = face(true)
        value["deck"] = R.string(card["deck"]); value["reason"] = R.string(card["reason"])
        value["local_id"] = id + ":" + String(index); value["entity_id"] = id; value["entity_index"] = index
        value["source_ref"] = ["sourceId", "documentId", "bookId", "url"].map { R.string(source[$0]) }.first(where: { !$0.isEmpty }) ?? ""
        value["source_url"] = R.string(source["url"]); value["source"] = source
        value["_localReview"] = ["gid": id, "cardIndex": index, "contentKeys": card.keys.sorted(),
            "entityRev": try R.integer(record["entityRev"] ?? 0, "entity revision"),
            "stateRev": try R.integer(record["stateRev"] ?? 0, "state revision"),
            "review": state["review"] as? O ?? [:], "projections": state["projections"] as? O ?? [:],
            "wasDue": entry["due"] as? Bool == true] as O
        if let receipt = ((state["projections"] as? O)?["anki"] as? O)?["pi-legacy"] as? O,
           receipt["status"] as? String == "succeeded", let ids = receipt["cardIds"] as? [Any], ids.count == 1,
           let external = try? R.integer(ids[0], "Anki card id"), external > 0 {
            value["_legacyExternalCardId"] = external
        }
        return value
    }
}
