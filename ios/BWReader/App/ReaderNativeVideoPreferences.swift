import Foundation

/// Preserve the existing device-scoped video preference record. The player
/// submits patches; it never creates a second preference store.
struct ReaderNativeVideoPreferences {
    let store: ReaderNativeDataStore
    let deviceID: String
    func request(method: String, body: String) throws -> [String: Any] {
        let collection = "native-video-player-prefs", id = deviceID + ":video-player-prefs"
        return try store.inTransaction {
            let previous = try store.record(collection: collection, id: id)
            var prefs: [String: Any] = [:]
            if let previous, !previous.deleted {
                let envelope = try ReaderNativeCardRules.object(JSONSerialization.jsonObject(with: Data(previous.json.utf8)), "video preferences")
                guard let value = envelope["value"] as? [String: Any], value["id"] as? String == id,
                      value["deviceId"] as? String == deviceID, let payload = value["payload"] as? [String: Any] else {
                    throw ReaderNativePreferences.Failure(message: "播放器设置记录损坏")
                }
                prefs = payload
            }
            if method == "POST" {
                let input = try ReaderNativeCardRules.object(JSONSerialization.jsonObject(with: Data(body.utf8)), "video preference patch")
                guard Set(input.keys) == ["patch"], let patch = input["patch"] as? [String: Any] else {
                    throw ReaderNativePreferences.Failure(message: "播放器设置无效")
                }
                for (key, value) in patch {
                    guard ["x", "y", "w", "h", "showEn", "subOut"].contains(key) else { throw ReaderNativePreferences.Failure(message: "播放器设置字段无效") }
                    if value is NSNull { prefs.removeValue(forKey: key); continue }
                    if ["showEn", "subOut"].contains(key) { _ = try ReaderNativeCardRules.bool(value, key) }
                    else {
                        let number = try ReaderNativeCardRules.number(value, key)
                        guard number >= -100_000, number <= 100_000 else { throw ReaderNativePreferences.Failure(message: "播放器尺寸无效") }
                    }
                    prefs[key] = value
                }
                let revision = previous?.rev ?? 0, stamp = Int64(Date().timeIntervalSince1970 * 1000)
                guard revision < 9_007_199_254_740_991 else { throw ReaderNativePreferences.Failure(message: "播放器设置版本超出上限") }
                let mutation = "native-video-prefs-" + UUID().uuidString
                let record: [String: Any] = ["schema": 1, "collection": collection, "id": id, "rev": revision + 1,
                    "updatedAt": stamp, "updatedBy": deviceID, "deleted": false,
                    "value": ["id": id, "deviceId": deviceID, "payload": prefs, "updatedAt": stamp]]
                let raw = try ReaderNativeCardRules.bytes(record)
                _ = try store.commitWithinTransaction(record: .init(collection: collection, id: id, rev: revision + 1,
                    updatedAt: stamp, deleted: false, json: String(decoding: raw, as: UTF8.self)), mutationId: mutation,
                    journalJSON: { cursor in String(decoding: try! ReaderNativeCardRules.bytes([
                        "cursor": cursor, "mutationId": mutation, "operation": "put", "collection": collection, "record": record
                    ]), as: UTF8.self) }, expectedRev: revision, now: stamp)
            } else if method != "GET" { throw ReaderNativePreferences.Failure(message: "不支持的播放器操作") }
            return ["ok": true, "prefs": prefs]
        }
    }
}
