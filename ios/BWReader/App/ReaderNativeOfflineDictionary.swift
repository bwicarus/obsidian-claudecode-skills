import Foundation

/// File IO, digest verification and JSON decoding are serialized off the UI
/// actor. Install replacement/removal invalidates every cached shard.
actor ReaderNativeOfflineDictionary {
    private let dictionary = ReaderNativeJapaneseDictionary(read: ReaderOfflineDictionaryStore.readRuntimeResource)
    private var installation: ReaderOfflineDictionaryInfo?

    func lookup(_ term: String, legacy: Bool = true) throws -> Data {
        let current = try ReaderOfflineDictionaryStore.installedInfo()
        if current != installation { dictionary.clear(); installation = current }
        let value: [String: Any]
        if current == nil {
            value = ["ok": false, "unavailable": true, "source": "local-jmdict", "code": "BW_OFFLINE_DICTIONARY_NOT_INSTALLED"]
        } else { value = try dictionary.lookup(term, legacy: legacy) }
        return try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }
}
