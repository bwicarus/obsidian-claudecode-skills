import Foundation

/// A panel's explicit Anki operation has one durable identity. Unknown network
/// outcomes remain pending; reopening/retrying that operation cannot resend it.
@MainActor
final class ReaderNativeLookupMutation {
    typealias Object = [String: Any]
    struct Response { let status: Int; let data: Data }
    private let read: (String) throws -> String?
    private let write: (String, String) throws -> Void
    private let send: (Data) async throws -> Response
    init(read: @escaping (String) throws -> String?, write: @escaping (String, String) throws -> Void,
         send: @escaping (Data) async throws -> Response) {
        self.read = read; self.write = write; self.send = send
    }

    func run(word: String, operation: String) async throws -> Object {
        guard !word.isEmpty, word.utf16.count <= 2000, !word.contains("\0"), UUID(uuidString: operation) != nil else {
            throw ReaderNativeLookupRequest.Failure(message: "Anki 请求无效")
        }
        let key = "native-vocab-anki:" + operation.lowercased()
        if let saved = try read(key) {
            guard let value = try JSONSerialization.jsonObject(with: Data(saved.utf8)) as? Object,
                  value["word"] as? String == word else { throw ReaderNativeLookupRequest.Failure(message: "Anki 请求记录不匹配") }
            if value["state"] as? String == "done", let result = value["result"] as? Object { return result }
            throw ReaderNativeLookupRequest.Failure(message: "上次 Anki 发送结果待确认，未重复发送；请先核对卡片")
        }
        func save(_ state: String, result: Object? = nil) throws {
            var value: Object = ["word": word, "state": state, "operation": operation]
            if let result { value["result"] = result }
            try write(key, String(decoding: JSONSerialization.data(withJSONObject: value), as: UTF8.self))
        }
        try Task.checkCancellation()
        let body = try JSONSerialization.data(withJSONObject: ["word": word])
        try save("pending")
        do {
            let response = try await send(body)
            guard (200..<300).contains(response.status),
                  let value = try JSONSerialization.jsonObject(with: response.data) as? Object,
                  value["ok"] as? Bool == true else {
                throw ReaderNativeLookupRequest.Failure(message: "服务器未确认 Anki 写入")
            }
            let result: Object = ["mode": "vocab-anki", "action": value["action"] as? String ?? "created",
                                  "note_id": value["note_id"] ?? NSNull()]
            // Even if the panel/book changed, persist the actual receipt.
            try save("done", result: result)
            return result
        } catch {
            throw ReaderNativeLookupRequest.Failure(message: "Anki 发送结果未确认，未自动重试：" + error.localizedDescription)
        }
    }
}
