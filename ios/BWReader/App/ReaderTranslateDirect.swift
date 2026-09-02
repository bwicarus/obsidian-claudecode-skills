import CryptoKit
import Foundation
import Security

/// 翻译二期（用户 2026-09-02 拍板 A 方案）：App 直连 Google Cloud Translation v2。
/// 密钥由 Windows 桥下发（Tailscale 内网 + 身份头），存 Keychain（本机、解锁后可读），
/// 不进构建、不进 git；换 key 只改 Windows 一处，App 端 24h 重拉一次，401/403 立即丢弃
/// 缓存重拉。三层缓存的前两层都在这里（页面 CSP 不许直连桥，契约 native-local-library
/// 钉死）：先查桥留底，miss 才直连，成功后推桥留底供全设备共享；失败 runtime 回退 Pi。
enum ReaderTranslateDirectError: Error {
    case keyUnavailable
    case upstream(Int)
    case emptyResult
}

actor ReaderTranslateDirectService {
    static let shared = ReaderTranslateDirectService()

    private static let keyURL = URL(
        string: "https://bwicarus-2.taile44d0c.ts.net/reader-translate-key")!
    private static let endpoint = URL(
        string: "https://translation.googleapis.com/language/translate/v2")!
    private static let cacheURL = URL(
        string: "https://bwicarus-2.taile44d0c.ts.net/reader-translate-cache")!
    private static let keyRefreshInterval: TimeInterval = 24 * 3600
    private static let keyFailureCooldown: TimeInterval = 120

    private var cachedKey: String?
    private var keyLoadedAt: Date?
    private var keyFailedAt: Date?

    /// 与 translate.py `_cache_path`（无 ns 形态）和桥 /reader-translate-cache 同键：
    /// sha1("zh-CN::" + text) 前 16 位。Windows 上翻过的句子 App 直接命中。
    static func cacheSha(text: String, target: String) -> String {
        let normalizedTarget = target.lowercased().hasPrefix("zh") ? "zh-CN" : target
        let digest = Insecure.SHA1.hash(data: Data((normalizedTarget + "::" + text).utf8))
        return digest.map { String(format: "%02x", $0) }.joined().prefix(16).description
    }

    func cachedTranslation(sha: String) async -> String? {
        guard var components = URLComponents(
            url: Self.cacheURL, resolvingAgainstBaseURL: false) else { return nil }
        components.queryItems = [URLQueryItem(name: "sha", value: sha)]
        guard let url = components.url else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 6
        // guard 条件里不能对 optional 元组解构,先整体绑定再取分量。
        guard let result = try? await URLSession.shared.data(for: request),
              (result.1 as? HTTPURLResponse)?.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: result.0) as? [String: Any],
              let translated = (object["tr"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !translated.isEmpty else {
            return nil
        }
        return translated
    }

    /// first-write-wins 由桥端保证；这里 fire-and-forget，失败不影响翻译主路。
    func storeTranslation(sha: String, source: String, translated: String, target: String) async {
        let normalizedTarget = target.lowercased().hasPrefix("zh") ? "zh-CN" : target
        var request = URLRequest(url: Self.cacheURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 6
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "sha": sha, "src": source, "tr": translated,
            "target": normalizedTarget, "source": "app-google",
        ])
        _ = try? await URLSession.shared.data(for: request)
    }

    func translate(_ text: String, target: String) async throws -> String {
        let key = try await apiKey()
        var request = URLRequest(url: Self.endpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 12
        request.setValue(
            "application/x-www-form-urlencoded; charset=utf-8",
            forHTTPHeaderField: "Content-Type"
        )
        // 与 translate.py 的 _gtranslate 同款参数：source 不指定让 Google 自动检测，
        // target 归一为 zh-CN，format=text（不按 HTML 解析）。
        let normalizedTarget = target.lowercased().hasPrefix("zh") ? "zh-CN" : target
        request.httpBody = Self.formEncode([
            ("q", text),
            ("target", normalizedTarget),
            ("format", "text"),
            ("key", key),
        ]).data(using: .utf8)
        let (data, reply) = try await URLSession.shared.data(for: request)
        let status = (reply as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else {
            if status == 401 || status == 403 {
                // key 失效或未放行 Translation API：丢掉缓存，下次调用重拉 ——
                // Windows 换 key 后 App 自动跟上，不需要出新包。
                cachedKey = nil
                keyLoadedAt = nil
                ReaderTranslateKeychain.delete()
            }
            throw ReaderTranslateDirectError.upstream(status)
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let payload = object["data"] as? [String: Any],
              let translations = payload["translations"] as? [[String: Any]],
              let first = translations.first,
              let translated = (first["translatedText"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              !translated.isEmpty else {
            throw ReaderTranslateDirectError.emptyResult
        }
        return translated
    }

    private func apiKey() async throws -> String {
        if let cachedKey, let keyLoadedAt,
           Date().timeIntervalSince(keyLoadedAt) < Self.keyRefreshInterval {
            return cachedKey
        }
        if cachedKey == nil, let stored = ReaderTranslateKeychain.load() {
            cachedKey = stored
            keyLoadedAt = Date()
            return stored
        }
        if let keyFailedAt,
           Date().timeIntervalSince(keyFailedAt) < Self.keyFailureCooldown,
           let cachedKey {
            // 桥刚失败过：用手里的旧 key 顶着，别每句翻译都去撞桥。
            return cachedKey
        }
        do {
            let fetched = try await fetchKeyFromBridge()
            cachedKey = fetched
            keyLoadedAt = Date()
            keyFailedAt = nil
            ReaderTranslateKeychain.save(fetched)
            return fetched
        } catch {
            keyFailedAt = Date()
            if let cachedKey { return cachedKey }
            throw ReaderTranslateDirectError.keyUnavailable
        }
    }

    private func fetchKeyFromBridge() async throws -> String {
        var request = URLRequest(url: Self.keyURL)
        request.httpMethod = "GET"
        request.timeoutInterval = 10
        let (data, reply) = try await URLSession.shared.data(for: request)
        let status = (reply as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              (object["ok"] as? Bool) == true,
              let key = (object["key"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
              (20...200).contains(key.count) else {
            throw ReaderTranslateDirectError.keyUnavailable
        }
        return key
    }

    private static func formEncode(_ fields: [(String, String)]) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        return fields.map { field -> String in
            let encoded = field.1.addingPercentEncoding(withAllowedCharacters: allowed) ?? ""
            return field.0 + "=" + encoded
        }.joined(separator: "&")
    }
}

/// Keychain 里只有这一条：service/account 固定，本机、解锁后可读。
private enum ReaderTranslateKeychain {
    private static let service = "space.bwicarus.reader.translate"
    private static let account = "google-translate-key"

    private static func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    static func load() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8),
              !value.isEmpty else {
            return nil
        }
        return value
    }

    static func save(_ value: String) {
        guard let data = value.data(using: .utf8) else { return }
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(
            baseQuery() as CFDictionary,
            attributes as CFDictionary
        )
        if updateStatus == errSecItemNotFound {
            var add = baseQuery()
            for (attributeKey, attributeValue) in attributes {
                add[attributeKey] = attributeValue
            }
            _ = SecItemAdd(add as CFDictionary, nil)
        }
    }

    static func delete() {
        _ = SecItemDelete(baseQuery() as CFDictionary)
    }
}
