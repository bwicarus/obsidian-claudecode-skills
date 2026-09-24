import Foundation
import CoreFoundation

/// Data requests made by native lookup panels. Browser clients retain their
/// own adapter; App responses preserve its fields and language routing.
struct ReaderNativeLookupRequest {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    let mode: String
    let text: String
    let path: String
    let method: String
    let body: Data

    static func isJapanese(_ text: String, languages: [String]) -> Bool {
        if text.unicodeScalars.contains(where: { (0x3040...0x30ff).contains($0.value) }) { return true }
        guard text.unicodeScalars.contains(where: { (0x3400...0x9fff).contains($0.value) }) else { return false }
        return languages.isEmpty || languages.contains("ja")
    }

    init(_ input: [String: Any], file: String, languages: [String]) throws {
        guard let mode = input["mode"] as? String, ["translate", "example-zh", "dict-full"].contains(mode),
              let original = input["text"] as? String else { throw Failure(message: "BW_READER_LOOKUP_TEXT") }
        let text = original.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.utf16.count <= 2000, !text.contains("\0") else { throw Failure(message: "BW_READER_LOOKUP_TEXT") }
        self.mode = mode; self.text = text
        if mode == "dict-full" {
            guard !Self.isJapanese(text, languages: languages) else { throw Failure(message: "BW_READER_LOOKUP_JP_FULL") }
            var parts = URLComponents()
            parts.path = "/pdf/api/dict"
            let page = (input["page"] as? NSNumber).flatMap { value -> Int? in
                guard CFGetTypeID(value) != CFBooleanGetTypeID(), value.doubleValue.isFinite,
                      value.doubleValue >= 0, value.doubleValue <= 9_007_199_254_740_991,
                      value.doubleValue.rounded() == value.doubleValue else { return nil }
                return value.intValue
            } ?? 0
            let context = String((input["context"] as? String ?? "").prefix(320))
            parts.queryItems = [.init(name: "word", value: text), .init(name: "file", value: file),
                                .init(name: "page", value: String(page)), .init(name: "context", value: context)]
            // Flask query decoding uses form semantics: literal plus must not
            // become a space in the selected word or its sentence context.
            parts.percentEncodedQuery = parts.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
            guard let value = parts.string else { throw Failure(message: "BW_READER_LOOKUP_TEXT") }
            path = value; method = "GET"; body = Data()
        } else {
            path = "/pdf/api/translate-sentence"; method = "POST"
            var fields = ["text": text]
            if mode == "example-zh" { fields["backend"] = "ai" }
            body = try JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
        }
    }

    /// Preserve the App's direct translation path before the authorized server
    /// fallback. Cancellation must not start a new remote request.
    func response(directTranslation: () async throws -> String,
                  remote: () async throws -> (status: Int, data: Data)) async throws -> [String: Any] {
        try Task.checkCancellation()
        if mode == "translate" || mode == "example-zh" {
            do {
                let translated = try await directTranslation()
                guard !translated.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                    throw Failure(message: "BW_READER_TRANSLATE_EMPTY")
                }
                try Task.checkCancellation()
                return try decode(status: 200, data: JSONSerialization.data(withJSONObject: ["ok": true, "zh": translated]))
            } catch {
                if error is CancellationError || Task.isCancelled { throw CancellationError() }
            }
        }
        try Task.checkCancellation()
        let value = try await remote()
        try Task.checkCancellation()
        return try decode(status: value.status, data: value.data)
    }

    func decode(status: Int, data: Data) throws -> [String: Any] {
        guard (200..<300).contains(status),
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let ok = object["ok"] as? NSNumber, CFGetTypeID(ok) == CFBooleanGetTypeID(), ok.boolValue else {
            throw Failure(message: mode == "dict-full" ? "BW_READER_LOOKUP_MISS" : "BW_READER_TRANSLATE_FAILED")
        }
        if mode != "dict-full" {
            let zh = object["zh"] as? String ?? ""
            if mode == "example-zh" {
                let trimmed = zh.trimmingCharacters(in: .whitespacesAndNewlines)
                return ["mode": mode, "zh": trimmed.unicodeScalars.contains(where: { (0x3400...0x9fff).contains($0.value) }) ? trimmed : ""]
            }
            return ["mode": mode, "text": text, "zh": zh]
        }
        return ["mode": "dict", "full": true, "jp": false, "word": object["word"] as? String ?? text,
                "lemma": object["lemma"] as? String ?? "", "phonetic": object["phonetic"] as? String ?? "",
                "translation": object["translation"] as? String ?? "",
                "definition": String((object["definition"] as? String ?? "").prefix(4000)),
                "examples": Array((object["examples"] as? [Any] ?? []).prefix(6)),
                "synonyms": Array((object["synonyms"] as? [Any] ?? []).prefix(8)),
                "antonyms": Array((object["antonyms"] as? [Any] ?? []).prefix(8))]
    }
}
