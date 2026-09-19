import Foundation

actor MarketCache {
    static let shared = MarketCache()

    private struct Envelope<Value: Codable>: Codable {
        let savedAt: Date
        let value: Value
    }

    private let directory: URL
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    private init() {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first!
        directory = base.appendingPathComponent("StocksNativeMarket", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    func value<Value: Codable>(_ type: Value.Type, for key: String, maxAge: TimeInterval) -> Value? {
        let url = fileURL(key)
        guard let data = try? Data(contentsOf: url),
              let envelope = try? decoder.decode(Envelope<Value>.self, from: data),
              Date().timeIntervalSince(envelope.savedAt) <= maxAge else { return nil }
        try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: url.path)
        return envelope.value
    }

    func save<Value: Codable>(_ value: Value, for key: String) {
        guard let data = try? encoder.encode(Envelope(savedAt: Date(), value: value)) else { return }
        try? data.write(to: fileURL(key), options: .atomic)
    }

    func clean(maxAge: TimeInterval = 14 * 24 * 3600, maxBytes: Int64 = 80 * 1024 * 1024) {
        let manager = FileManager.default
        let keys: Set<URLResourceKey> = [.contentModificationDateKey, .fileSizeKey, .isRegularFileKey]
        guard var files = try? manager.contentsOfDirectory(at: directory, includingPropertiesForKeys: Array(keys)) else { return }
        let now = Date()
        files.removeAll { url in
            guard let values = try? url.resourceValues(forKeys: keys), values.isRegularFile == true else { return true }
            if let date = values.contentModificationDate, now.timeIntervalSince(date) > maxAge {
                try? manager.removeItem(at: url)
                return true
            }
            return false
        }
        var sized = files.compactMap { url -> (URL, Date, Int64)? in
            guard let values = try? url.resourceValues(forKeys: keys) else { return nil }
            return (url, values.contentModificationDate ?? .distantPast, Int64(values.fileSize ?? 0))
        }.sorted { $0.1 < $1.1 }
        var total = sized.reduce(Int64(0)) { $0 + $1.2 }
        while total > maxBytes, !sized.isEmpty {
            let oldest = sized.removeFirst()
            try? manager.removeItem(at: oldest.0)
            total -= oldest.2
        }
    }

    private func fileURL(_ key: String) -> URL {
        let safe = key.map { character in
            character.isLetter || character.isNumber || character == "-" ? character : "_"
        }
        return directory.appendingPathComponent(String(safe) + ".json")
    }
}
