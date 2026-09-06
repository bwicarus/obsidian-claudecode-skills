import Foundation
import Security

/// 服务器账号的设备令牌（Bearer token）。
///
/// 用户 2026-09-06："就不能设计为 App 登录后扩展自动登录么"。App 登录服务器之后铸一枚
/// 设备令牌存进这里；Safari 扩展的 native handler 经 native messaging 读走，再走跟手工
/// 粘贴完全相同的校验与保存路径。用户从此不需要去 /profile/ 复制令牌。
///
/// 存放：与 Realtime 凭证同一个签名 Keychain 访问组 —— App 写，App 与扩展的原生进程读；
/// 网页 / 扩展 JS 只拿到 background 账户存储里的副本，从不落到页面。
struct ReaderStoredAccountToken: Codable, Sendable {
    let contract: String
    let origin: String
    let token: String
    let label: String
    let mintedAt: Date
}

enum ReaderAccountTokenError: LocalizedError {
    case unavailable
    case invalidToken
    case notLoggedIn
    case mintFailed(String)
    case keychain(OSStatus)

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "BWReader App 尚未登录服务器"
        case .invalidToken:
            return "服务器返回的设备令牌格式无效"
        case .notLoggedIn:
            return "App 里没有服务器登录会话"
        case .mintFailed(let detail):
            return "铸设备令牌失败：\(detail)"
        case .keychain(let status):
            return "Apple Keychain 操作失败（\(status)）"
        }
    }
}

final class ReaderAccountTokenStore: @unchecked Sendable {
    static let shared = ReaderAccountTokenStore()
    static let contract = "reader-account-token/1"

    private let service = "space.bwicarus.bwreader2.account-token"
    private let account = "default"

    private init() {}

    /// 服务端 `secrets.token_urlsafe(32)` 只出 URL-safe base64 字符；长度上限与扩展侧一致（8192）。
    static func isValidToken(_ value: String) -> Bool {
        guard (16...8192).contains(value.utf8.count) else { return false }
        return value.unicodeScalars.allSatisfy { scalar in
            switch scalar.value {
            case 45, 46, 48...57, 61, 65...90, 95, 97...122, 126:
                return true
            default:
                return false
            }
        }
    }

    func loadIfPresent() throws -> ReaderStoredAccountToken? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else {
            throw ReaderAccountTokenError.keychain(status)
        }
        guard let data = item as? Data,
              let stored = try? JSONDecoder().decode(
                ReaderStoredAccountToken.self,
                from: data
              ),
              stored.contract == Self.contract,
              stored.origin.hasPrefix("https://"),
              Self.isValidToken(stored.token)
        else {
            throw ReaderAccountTokenError.invalidToken
        }
        return stored
    }

    func save(origin: String, token: String, label: String) throws {
        guard Self.isValidToken(token), origin.hasPrefix("https://") else {
            throw ReaderAccountTokenError.invalidToken
        }
        let stored = ReaderStoredAccountToken(
            contract: Self.contract,
            origin: origin,
            token: token,
            label: String(label.prefix(80)),
            mintedAt: Date()
        )
        let data = try JSONEncoder().encode(stored)
        let query = baseQuery()
        let updates: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String:
                kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(
            query as CFDictionary,
            updates as CFDictionary
        )
        if updateStatus == errSecSuccess { return }
        guard updateStatus == errSecItemNotFound else {
            throw ReaderAccountTokenError.keychain(updateStatus)
        }
        var add = query
        updates.forEach { add[$0.key] = $0.value }
        let addStatus = SecItemAdd(add as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw ReaderAccountTokenError.keychain(addStatus)
        }
    }

    func clear() throws {
        let status = SecItemDelete(baseQuery() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw ReaderAccountTokenError.keychain(status)
        }
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrAccessGroup as String:
                ReaderNativeBridgeContract.realtimeKeychainAccessGroup,
        ]
    }
}
