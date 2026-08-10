import Foundation
import Security

struct ReaderRealtimeCredentialStatus: Equatable, Sendable {
    let isConfigured: Bool
    let importedAt: Date?
    let model: String

    static let missing = ReaderRealtimeCredentialStatus(
        isConfigured: false,
        importedAt: nil,
        model: ""
    )
}

struct ReaderStoredRealtimeCredential: Codable, Sendable {
    let contract: String
    let apiKey: String
    let sessionJSON: Data
    let model: String
    let rtImage: Bool
    let compactTokens: Int
    let safetyIdentifier: String
    let importedAt: Date
}

enum ReaderRealtimeCredentialError: LocalizedError {
    case unavailable
    case invalidCredential
    case keychain(OSStatus)
    case loginRequired
    case server(String)
    case invalidResponse
    case openAI(String)
    case invalidRequest
    case imageTooLarge
    case imageRejected(String)

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "尚未在 BWReader App 中导入现有 OpenAI Key"
        case .invalidCredential:
            return "导入的 OpenAI Realtime 凭证无效"
        case .keychain(let status):
            return "Apple Keychain 操作失败（\(status)）"
        case .loginRequired:
            return "请先在下方登录 Pi，再同步现有 Realtime 语音设置"
        case .server(let message):
            return message
        case .invalidResponse:
            return "Realtime 服务返回了无效响应"
        case .openAI(let message):
            return message
        case .invalidRequest:
            return "Realtime 原生请求无效"
        case .imageTooLarge:
            return "当前合成图超过 App 的安全大小上限"
        case .imageRejected(let message):
            return message
        }
    }
}

/// The containing App is the only UI that can write this credential. The App
/// and Safari extension native processes can read the same item through their
/// signed Keychain access group; page/background JavaScript never receives it.
final class ReaderRealtimeCredentialStore: @unchecked Sendable {
    static let shared = ReaderRealtimeCredentialStore()

    private let service = "space.bwicarus.bwreader2.openai-realtime"
    private let account = "default"

    private init() {}

    func status() throws -> ReaderRealtimeCredentialStatus {
        guard let value = try loadIfPresent() else { return .missing }
        return ReaderRealtimeCredentialStatus(
            isConfigured: true,
            importedAt: value.importedAt,
            model: value.model
        )
    }

    func load() throws -> ReaderStoredRealtimeCredential {
        guard let value = try loadIfPresent() else {
            throw ReaderRealtimeCredentialError.unavailable
        }
        return value
    }

    func save(
        apiKey: String,
        sessionJSON: Data,
        model: String,
        rtImage: Bool,
        compactTokens: Int
    ) throws {
        guard Self.isValidProjectKey(apiKey),
              sessionJSON.count <= 1_048_576,
              let session = try? JSONSerialization.jsonObject(
                with: sessionJSON
              ) as? [String: Any],
              session["type"] as? String == "realtime",
              let sessionModel = session["model"] as? String,
              !sessionModel.isEmpty,
              sessionModel.utf8.count <= 160
        else {
            throw ReaderRealtimeCredentialError.invalidCredential
        }
        let stored = ReaderStoredRealtimeCredential(
            contract: "reader-native-realtime-keychain/2",
            apiKey: apiKey,
            sessionJSON: sessionJSON,
            model: model.isEmpty ? sessionModel : model,
            rtImage: rtImage,
            compactTokens: max(0, min(compactTokens, 1_000_000)),
            safetyIdentifier: "bwreader-\(UUID().uuidString.lowercased())",
            importedAt: Date()
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
            throw ReaderRealtimeCredentialError.keychain(updateStatus)
        }
        var add = query
        updates.forEach { add[$0.key] = $0.value }
        let addStatus = SecItemAdd(add as CFDictionary, nil)
        guard addStatus == errSecSuccess else {
            throw ReaderRealtimeCredentialError.keychain(addStatus)
        }
    }

    func clear() throws {
        let status = SecItemDelete(baseQuery() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw ReaderRealtimeCredentialError.keychain(status)
        }
    }

    private func loadIfPresent() throws -> ReaderStoredRealtimeCredential? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess,
              let data = item as? Data,
              let stored = try? JSONDecoder().decode(
                ReaderStoredRealtimeCredential.self,
                from: data
              ),
              stored.contract == "reader-native-realtime-keychain/2",
              Self.isValidProjectKey(stored.apiKey),
              stored.sessionJSON.count <= 1_048_576,
              Self.isValidSafetyIdentifier(stored.safetyIdentifier)
        else {
            if status != errSecSuccess {
                throw ReaderRealtimeCredentialError.keychain(status)
            }
            throw ReaderRealtimeCredentialError.invalidCredential
        }
        return stored
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

    private static func isValidProjectKey(_ value: String) -> Bool {
        value.hasPrefix("sk-")
            && (20...512).contains(value.utf8.count)
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && !$0.properties.isWhitespace
            }
    }

    private static func isValidSafetyIdentifier(_ value: String) -> Bool {
        value.hasPrefix("bwreader-")
            && (20...96).contains(value.utf8.count)
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && (
                    CharacterSet.alphanumerics.contains($0)
                        || $0 == "-" || $0 == "_"
                )
            }
    }
}

enum ReaderRealtimeOpenAIClient {
    struct SessionConfiguration: Sendable {
        let sessionJSON: Data
        let model: String
        let rtImage: Bool
        let compactTokens: Int
    }

    struct MintedCredential: Sendable {
        let clientSecret: String
        let expiresAt: Int
        let model: String
        let rtImage: Bool
        let compactTokens: Int
    }

    private static let piOrigin = URL(
        string: "https://bwicarus.taile44d0c.ts.net"
    )!
    private static let openAIOrigin = URL(
        string: "https://api.openai.com"
    )!

    static func fetchSessionConfigurationFromPi(
        cookies: [HTTPCookie]
    ) async throws -> SessionConfiguration {
        let url = piOrigin.appendingPathComponent(
            "api/assistant/native-realtime-config"
        )
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.httpShouldHandleCookies = false
        request.timeoutInterval = 25
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "contract": "reader-native-realtime-config/1",
        ])
        let matchingCookies = eligibleCookies(for: url, from: cookies)
        HTTPCookie.requestHeaderFields(with: matchingCookies).forEach {
            request.setValue($0.value, forHTTPHeaderField: $0.key)
        }
        let (data, response) = try await ephemeralSession().data(for: request)
        guard let http = response as? HTTPURLResponse,
              http.url?.scheme == url.scheme,
              http.url?.host == url.host,
              http.url?.port == url.port else {
            throw ReaderRealtimeCredentialError.invalidResponse
        }
        if http.statusCode == 401 {
            throw ReaderRealtimeCredentialError.loginRequired
        }
        guard (200..<300).contains(http.statusCode) else {
            throw ReaderRealtimeCredentialError.server(
                safeErrorMessage(
                    from: data,
                    fallback: "Pi 语音设置同步失败（HTTP \(http.statusCode)）"
                )
            )
        }
        guard data.count <= 1_500_000,
              let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              object["ok"] as? Bool == true,
              object["contract"] as? String ==
                "reader-native-realtime-config/1",
              let session = object["session"] as? [String: Any],
              JSONSerialization.isValidJSONObject(session)
        else {
            throw ReaderRealtimeCredentialError.invalidResponse
        }
        let sessionJSON = try JSONSerialization.data(
            withJSONObject: session,
            options: [.sortedKeys]
        )
        return SessionConfiguration(
            sessionJSON: sessionJSON,
            model: object["model"] as? String ?? "",
            rtImage: object["rt_image"] as? Bool ?? true,
            compactTokens: (object["compact_tokens"] as? NSNumber)?.intValue
                ?? 0
        )
    }

    static func mintClientSecret() async throws -> MintedCredential {
        let stored = try ReaderRealtimeCredentialStore.shared.load()
        guard let session = try JSONSerialization.jsonObject(
            with: stored.sessionJSON
        ) as? [String: Any] else {
            throw ReaderRealtimeCredentialError.invalidCredential
        }
        let url = openAIOrigin.appendingPathComponent(
            "v1/realtime/client_secrets"
        )
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 25
        request.httpShouldHandleCookies = false
        request.setValue(
            "Bearer \(stored.apiKey)",
            forHTTPHeaderField: "Authorization"
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.setValue(
            stored.safetyIdentifier,
            forHTTPHeaderField: "OpenAI-Safety-Identifier"
        )
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "expires_after": ["anchor": "created_at", "seconds": 90],
            "session": session,
        ])
        let (data, response) = try await ephemeralSession().data(for: request)
        guard let http = response as? HTTPURLResponse,
              http.url?.scheme == url.scheme,
              http.url?.host == url.host,
              http.url?.port == url.port else {
            throw ReaderRealtimeCredentialError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            throw ReaderRealtimeCredentialError.openAI(
                safeErrorMessage(
                    from: data,
                    fallback: "OpenAI 临时凭证签发失败（HTTP \(http.statusCode)）"
                )
            )
        }
        guard data.count <= 256_000,
              let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let secret = object["value"] as? String,
              isValidClientSecret(secret) else {
            throw ReaderRealtimeCredentialError.invalidResponse
        }
        return MintedCredential(
            clientSecret: secret,
            expiresAt: (object["expires_at"] as? NSNumber)?.intValue ?? 0,
            model: stored.model,
            rtImage: stored.rtImage,
            compactTokens: stored.compactTokens
        )
    }

    static func injectImage(
        callID: String,
        clientSecret: String,
        mediaType: String,
        base64: String
    ) async throws {
        guard isValidCallID(callID),
              isValidClientSecret(clientSecret),
              ["image/jpeg", "image/png", "image/webp"].contains(mediaType),
              (3_000...2_800_000).contains(base64.utf8.count),
              let decoded = Data(base64Encoded: base64),
              (2_000...2_097_152).contains(decoded.count)
        else {
            throw ReaderRealtimeCredentialError.imageTooLarge
        }
        guard var components = URLComponents(
            string: "wss://api.openai.com/v1/realtime"
        ) else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        components.queryItems = [URLQueryItem(name: "call_id", value: callID)]
        guard let url = components.url else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 12
        request.setValue(
            "Bearer \(clientSecret)",
            forHTTPHeaderField: "Authorization"
        )
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        let session = URLSession(configuration: configuration)
        let socket = session.webSocketTask(with: request)
        socket.maximumMessageSize = 4 * 1_024 * 1_024
        socket.resume()
        defer {
            socket.cancel(with: .normalClosure, reason: nil)
            session.invalidateAndCancel()
        }
        let payload: [String: Any] = [
            "type": "conversation.item.create",
            "item": [
                "type": "message",
                "role": "user",
                "content": [[
                    "type": "input_image",
                    "detail": "high",
                    "image_url": "data:\(mediaType);base64,\(base64)",
                ]],
            ],
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        guard let text = String(data: data, encoding: .utf8) else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        try await socket.send(.string(text))
        try await waitForImageConfirmation(socket)
    }

    static func hangup(
        callID: String,
        clientSecret: String
    ) async throws {
        guard isValidCallID(callID), isValidClientSecret(clientSecret) else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        let url = openAIOrigin
            .appendingPathComponent("v1/realtime/calls")
            .appendingPathComponent(callID)
            .appendingPathComponent("hangup")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.httpShouldHandleCookies = false
        request.setValue(
            "Bearer \(clientSecret)",
            forHTTPHeaderField: "Authorization"
        )
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        let (_, response) = try await ephemeralSession().data(for: request)
        guard let http = response as? HTTPURLResponse,
              http.url?.scheme == url.scheme,
              http.url?.host == url.host,
              http.url?.port == url.port,
              (200..<300).contains(http.statusCode) else {
            throw ReaderRealtimeCredentialError.openAI(
                "OpenAI 挂断请求失败"
            )
        }
    }

    private static func waitForImageConfirmation(
        _ socket: URLSessionWebSocketTask
    ) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask {
                while !Task.isCancelled {
                    let message = try await socket.receive()
                    let text: String
                    switch message {
                    case .string(let value):
                        text = value
                    case .data(let value):
                        text = String(data: value, encoding: .utf8) ?? ""
                    @unknown default:
                        continue
                    }
                    guard let data = text.data(using: .utf8),
                          let event = try? JSONSerialization.jsonObject(
                            with: data
                          ) as? [String: Any],
                          let type = event["type"] as? String else {
                        continue
                    }
                    if type == "conversation.item.created" { return }
                    if type == "error" {
                        let detail = event["error"] as? [String: Any]
                        let code = (detail?["code"] as? String ?? "")
                            .prefix(80)
                        throw ReaderRealtimeCredentialError.imageRejected(
                            code.isEmpty
                                ? "OpenAI 未接受当前合成图"
                                : "OpenAI 未接受当前合成图（\(code)）"
                        )
                    }
                }
                throw ReaderRealtimeCredentialError.imageRejected(
                    "OpenAI 图像通道已关闭"
                )
            }
            group.addTask {
                try await Task.sleep(nanoseconds: 5_000_000_000)
                throw ReaderRealtimeCredentialError.imageRejected(
                    "OpenAI 图像确认超时"
                )
            }
            _ = try await group.next()
            group.cancelAll()
        }
    }

    private static func ephemeralSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }

    private static func eligibleCookies(
        for url: URL,
        from cookies: [HTTPCookie]
    ) -> [HTTPCookie] {
        guard let host = url.host?.lowercased() else { return [] }
        let path = url.path.isEmpty ? "/" : url.path
        let now = Date()
        return cookies.filter { cookie in
            let domain = cookie.domain.lowercased()
                .trimmingCharacters(in: CharacterSet(charactersIn: "."))
            let cookiePath = cookie.path.isEmpty ? "/" : cookie.path
            let domainMatches = host == domain || host.hasSuffix(".\(domain)")
            let pathMatches = path == cookiePath
                || (path.hasPrefix(cookiePath)
                    && (cookiePath.hasSuffix("/")
                        || path.dropFirst(cookiePath.count).first == "/"))
            return domainMatches
                && pathMatches
                && cookie.isSecure
                && (cookie.expiresDate.map { $0 > now } ?? true)
        }
    }

    private static func safeErrorMessage(
        from data: Data,
        fallback: String
    ) -> String {
        guard data.count <= 256_000,
              let object = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any] else {
            return fallback
        }
        if let message = object["error"] as? String,
           !message.isEmpty {
            return String(message.prefix(240))
        }
        if let error = object["error"] as? [String: Any],
           let message = error["message"] as? String,
           !message.isEmpty {
            return String(message.prefix(240))
        }
        return fallback
    }

    private static func isValidClientSecret(_ value: String) -> Bool {
        value.hasPrefix("ek_")
            && (11...4_096).contains(value.utf8.count)
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && (
                    CharacterSet.alphanumerics.contains($0)
                        || $0 == "_" || $0 == "-"
                )
            }
    }

    private static func isValidCallID(_ value: String) -> Bool {
        value.hasPrefix("rtc_")
            && (12...160).contains(value.utf8.count)
            && value.unicodeScalars.allSatisfy {
                $0.isASCII && (
                    CharacterSet.alphanumerics.contains($0)
                        || $0 == "_" || $0 == "-"
                )
            }
    }
}
