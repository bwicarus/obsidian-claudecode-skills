import Foundation
import Security

enum Credentials {
    private static let service = "space.bwicarus.stocksnative.auth"

    private static func query(baseURL: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: baseURL]
    }

    static func token(baseURL: String) -> String? {
        var request = query(baseURL: baseURL)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func save(token: String, baseURL: String) throws {
        let request = query(baseURL: baseURL)
        let attributes: [String: Any] = [
            kSecValueData as String: Data(token.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        ]
        let update = SecItemUpdate(request as CFDictionary, attributes as CFDictionary)
        if update == errSecItemNotFound {
            var newItem = request
            attributes.forEach { newItem[$0.key] = $0.value }
            let result = SecItemAdd(newItem as CFDictionary, nil)
            guard result == errSecSuccess else { throw AppError.message("无法安全保存设备凭证（\(result)）。") }
        } else if update != errSecSuccess {
            throw AppError.message("无法更新设备凭证（\(update)）。")
        }
    }

    static func delete(baseURL: String) { SecItemDelete(query(baseURL: baseURL) as CFDictionary) }
}
