#if os(watchOS)

import Foundation
import Security

/// 手表上那把语音桥 token 的存放处。
///
/// ## 为什么是 Keychain 而不是 UserDefaults / applicationContext
///
/// 这把 token 能启动电脑上的语音助手并双向串音频 —— **被偷等于一条通到
/// 用户 PC 的活麦**。`applicationContext` 会持久化，够用；但它是明文的
/// plist，跟"一条通向麦克风的凭证"不匹配。
///
/// ## ⚠ 它怎么来的
///
/// 手表上没有键盘也没有登录界面，所以 token **只能由别人送进来**：
///
///   手机（持有 Pi 的会话 cookie）→ GET /api/voice/watch-token
///     → WCSession 送到手表 → 存进这里
///
/// 也就是说**首次配好之前手表用不了语音**，而配好之后就不再依赖手机 ——
/// 这跟卡片/待办那条「手表是手机的镜子」的链路是两回事，别混。
enum WatchTokenStore {
    private static let service = "space.bwicarus.bwreader2.watchvoice"
    private static let account = "relay-token"

    /// 读。取不到就是取不到 —— **不给默认值**，也不返回空串冒充"有"。
    static func load() -> String? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess,
              let data = item as? Data,
              let token = String(data: data, encoding: .utf8),
              !token.isEmpty
        else { return nil }
        return token
    }

    /// 写。已存在就覆盖。
    ///
    /// - Returns: 成功与否。⚠ **调用方要看这个返回值**：Keychain 写失败是静默的，
    ///   而"以为存进去了其实没有"会在下次通话时表现为「按了没反应」。
    @discardableResult
    static func save(_ token: String) -> Bool {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 32 else { return false }   // 弱值不收，跟 Pi 侧同一条线
        guard let data = trimmed.data(using: .utf8) else { return false }

        var query = baseQuery()
        SecItemDelete(query as CFDictionary)              // 覆盖语义：先删再加
        query[kSecValueData as String] = data
        // 只在解锁后可读：这块表锁着的时候本来也发不起通话。
        query[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
    }

    static func clear() {
        SecItemDelete(baseQuery() as CFDictionary)
    }

    private static func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

#endif
