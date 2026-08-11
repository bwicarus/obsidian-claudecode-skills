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
    let safetyIdentifier: String
    let importedAt: Date
}

enum ReaderRealtimeCredentialError: LocalizedError {
    case unavailable
    case invalidCredential
    case keychain(OSStatus)
    case invalidResponse
    case openAI(String)
    case invalidRequest
    case invalidConfiguration
    case imageTooLarge
    case imageRejected(String)
    case visualCacheUnavailable

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "尚未在 BWReader App 中导入现有 OpenAI Key"
        case .invalidCredential:
            return "导入的 OpenAI Realtime 凭证无效"
        case .keychain(let status):
            return "Apple Keychain 操作失败（\(status)）"
        case .invalidResponse:
            return "Realtime 服务返回了无效响应"
        case .openAI(let message):
            return message
        case .invalidRequest:
            return "Realtime 原生请求无效"
        case .invalidConfiguration:
            return "Realtime 本机配置序列化精度无效"
        case .imageTooLarge:
            return "当前合成图超过 App 的安全大小上限"
        case .imageRejected(let message):
            return message
        case .visualCacheUnavailable:
            return "无法把当前合成图保存到 BWReader 本地缓存"
        }
    }
}

/// A bounded App Group cache for the exact composite handed to Realtime.
/// App and Safari Extension both write here before transmission, so a failed
/// network injection never turns the locally generated visual into a Pi-owned
/// artifact.  File names are opaque and contain no book, page, call or key.
private enum ReaderRealtimeVisualCache {
    private static let directoryName = "RealtimeVisuals"
    private static let filePrefix = "reader-visual-"
    private static let maximumFiles = 12

    static func store(_ data: Data, mediaType: String) throws {
        guard let root = FileManager.default.containerURL(
            forSecurityApplicationGroupIdentifier:
                ReaderNativeBridgeContract.appGroupIdentifier
        ) else {
            throw ReaderRealtimeCredentialError.visualCacheUnavailable
        }
        let directory = root
            .appendingPathComponent("NativeFeatures", isDirectory: true)
            .appendingPathComponent(directoryName, isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            let ext: String
            switch mediaType {
            case "image/png": ext = "png"
            case "image/webp": ext = "webp"
            default: ext = "jpg"
            }
            let timestamp = Int64(Date().timeIntervalSince1970 * 1_000)
            let name = filePrefix + String(timestamp) + "-" +
                UUID().uuidString.lowercased() + "." + ext
            try data.write(
                to: directory.appendingPathComponent(name, isDirectory: false),
                options: [.atomic]
            )
            prune(directory)
        } catch {
            throw ReaderRealtimeCredentialError.visualCacheUnavailable
        }
    }

    private static func prune(_ directory: URL) {
        let keys: Set<URLResourceKey> = [
            .contentModificationDateKey,
            .isRegularFileKey,
        ]
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: Array(keys),
            options: [.skipsHiddenFiles]
        ) else { return }
        let candidates = files.compactMap { url -> (URL, Date)? in
            guard url.lastPathComponent.hasPrefix(filePrefix),
                  let values = try? url.resourceValues(forKeys: keys),
                  values.isRegularFile == true else { return nil }
            return (url, values.contentModificationDate ?? .distantPast)
        }.sorted { $0.1 > $1.1 }
        for (url, _) in candidates.dropFirst(maximumFiles) {
            try? FileManager.default.removeItem(at: url)
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
            model: ReaderRealtimeOpenAIClient.model
        )
    }

    func load() throws -> ReaderStoredRealtimeCredential {
        guard let value = try loadIfPresent() else {
            throw ReaderRealtimeCredentialError.unavailable
        }
        return value
    }

    func save(apiKey: String) throws {
        guard Self.isValidProjectKey(apiKey) else {
            throw ReaderRealtimeCredentialError.invalidCredential
        }
        let stored = ReaderStoredRealtimeCredential(
            contract: "reader-native-realtime-keychain/3",
            apiKey: apiKey,
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
              [
                "reader-native-realtime-keychain/2",
                "reader-native-realtime-keychain/3",
              ].contains(stored.contract),
              Self.isValidProjectKey(stored.apiKey),
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

/// App-created Realtime calls use the project credential without ever exposing
/// it to page JavaScript. The page receives only this process-local capability;
/// Safari-extension calls that were created with an ephemeral key are not
/// registered here and keep using that same ephemeral identity.
private actor ReaderRealtimeProjectCallRegistry {
    static let shared = ReaderRealtimeProjectCallRegistry()

    private struct Entry {
        let capability: String
        let authorizationKey: String
        let createdAt: Date
    }

    private var entries = [String: Entry]()
    private let maximumEntries = 8
    private let maximumAge: TimeInterval = 12 * 60 * 60

    func register(callID: String, authorizationKey: String) -> String {
        prune()
        let capability = "ek_bwreader_" + UUID().uuidString
            .replacingOccurrences(of: "-", with: "")
            .lowercased()
        entries[callID] = Entry(
            capability: capability,
            authorizationKey: authorizationKey,
            createdAt: Date()
        )
        if entries.count > maximumEntries,
           let oldest = entries.min(by: {
               $0.value.createdAt < $1.value.createdAt
           })?.key {
            entries.removeValue(forKey: oldest)
        }
        return capability
    }

    /// Returns the bound project key for a registered App call, the ephemeral
    /// fallback for an extension-owned call, or nil for a forged capability.
    func authorizationKey(
        callID: String,
        capability: String,
        ephemeralFallback: String
    ) -> String? {
        prune()
        guard let entry = entries[callID] else {
            // An App capability whose registry entry expired or was evicted
            // must fail closed; it is not a real OpenAI ephemeral credential.
            return capability.hasPrefix("ek_bwreader_")
                ? nil
                : ephemeralFallback
        }
        guard entry.capability == capability else { return nil }
        return entry.authorizationKey
    }

    func remove(callID: String, capability: String) -> Bool {
        prune()
        guard entries[callID]?.capability == capability else { return false }
        entries.removeValue(forKey: callID)
        return true
    }

    private func prune(now: Date = Date()) {
        entries = entries.filter {
            now.timeIntervalSince($0.value.createdAt) <= maximumAge
        }
    }
}

enum ReaderRealtimeOpenAIClient {
    struct MintedCredential: Sendable {
        let clientSecret: String
        let expiresAt: Int
        let model: String
        let rtImage: Bool
        let compactTokens: Int
    }

    struct OpenedCall: Sendable {
        let answerSDP: String
        let callID: String
        let clientSecret: String
        let model: String
        let rtImage: Bool
        let compactTokens: Int
    }

    static let model = "gpt-realtime-2.1-mini"
    static let rtImage = true
    static let compactTokens = 0

    private static let openAIOrigin = URL(
        string: "https://api.openai.com"
    )!

    static func mintClientSecret() async throws -> MintedCredential {
        let stored = try ReaderRealtimeCredentialStore.shared.load()
        let session = localSessionConfiguration()
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
        request.httpBody = try clientSecretRequestBody(session: session)
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
            model: model,
            rtImage: rtImage,
            compactTokens: compactTokens
        )
    }

    /// WKWebView keeps the microphone and peer connection, while the signed
    /// App performs the credentialed SDP exchange. This avoids depending on
    /// Safari's cross-origin response-header behavior and keeps the complete
    /// Realtime startup failure visible to native code.
    static func openCall(sdp: String) async throws -> OpenedCall {
        guard sdp.hasPrefix("v=0"),
              (32...262_144).contains(sdp.utf8.count) else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        let stored = try ReaderRealtimeCredentialStore.shared.load()
        let sessionConfiguration = localSessionConfiguration()
        let url = openAIOrigin.appendingPathComponent("v1/realtime/calls")
        let boundary = "BWReaderRealtime-" + UUID().uuidString
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.httpShouldHandleCookies = false
        request.setValue(
            "Bearer \(stored.apiKey)",
            forHTTPHeaderField: "Authorization"
        )
        request.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        request.setValue("application/sdp", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        request.setValue(
            stored.safetyIdentifier,
            forHTTPHeaderField: "OpenAI-Safety-Identifier"
        )
        request.httpBody = try callRequestBody(
            sdp: sdp,
            session: sessionConfiguration,
            boundary: boundary
        )
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
                    fallback: "OpenAI Realtime 建连失败（HTTP \(http.statusCode)）"
                )
            )
        }
        let location = http.value(forHTTPHeaderField: "Location") ?? ""
        let callURL = URL(string: location, relativeTo: url)?.absoluteURL
        let callID = callURL?.lastPathComponent ?? ""
        guard callURL?.scheme == openAIOrigin.scheme,
              callURL?.host == openAIOrigin.host,
              callURL?.port == openAIOrigin.port,
              isValidCallID(callID),
              callURL?.path == "/v1/realtime/calls/\(callID)",
              (64...262_144).contains(data.count),
              let answer = String(data: data, encoding: .utf8),
              answer.hasPrefix("v=0") else {
            if isValidCallID(callID) {
                try? await hangupRequest(
                    callID: callID,
                    authorizationKey: stored.apiKey
                )
            }
            throw ReaderRealtimeCredentialError.invalidResponse
        }
        let capability = await ReaderRealtimeProjectCallRegistry.shared
            .register(callID: callID, authorizationKey: stored.apiKey)
        return OpenedCall(
            answerSDP: answer,
            callID: callID,
            clientSecret: capability,
            model: model,
            rtImage: rtImage,
            compactTokens: compactTokens
        )
    }

    /// The native App owns this complete, immutable baseline. No Pi request is
    /// needed to save a key or start a call, and the Safari extension receives
    /// the same session only through the signed native Keychain bridge.
    private static func localSessionConfiguration() -> [String: Any] {
        let empty: [String: Any] = [
            "type": "object",
            "properties": [String: Any](),
            "additionalProperties": false,
        ]
        let page: [String: Any] = [
            "type": "object",
            "properties": [
                "page": [
                    "anyOf": [["type": "integer"], ["type": "string"]],
                    "description": "要读取或查看的页；不给则使用当前页",
                ],
            ],
            "additionalProperties": false,
        ]
        let visual: [String: Any] = [
            "type": "object",
            "properties": [
                "page": [
                    "anyOf": [["type": "integer"], ["type": "string"]],
                ],
                "scope": [
                    "type": "string",
                    "enum": [
                        "viewport-context", "drawing-nearby", "selection-near",
                    ],
                ],
                "selectionId": ["type": "string"],
            ],
            "additionalProperties": false,
        ]
        let note: [String: Any] = [
            "type": "object",
            "properties": [
                "text": [
                    "type": "string",
                    "description": "要保存的笔记正文；不给则使用当前选中或当前可见内容",
                ],
                "title": [
                    "type": "string",
                    "description": "可选的简短笔记标题",
                ],
            ],
            "additionalProperties": false,
        ]
        let makeAnki: [String: Any] = [
            "type": "object",
            "properties": [
                "text": [
                    "type": "string",
                    "description": "制卡内容；不给则用当前选中或当前可见内容",
                ],
                "requirement": [
                    "type": "string",
                    "description": "用户对数量、难度、角度或语言的原始要求",
                ],
                "image_url": [
                    "type": "string",
                    "description": "可选配图 URL",
                ],
            ],
            "additionalProperties": false,
        ]
        let webSearch: [String: Any] = [
            "type": "object",
            "properties": [
                "query": ["type": "string"],
            ],
            "required": ["query"],
            "additionalProperties": false,
        ]
        let imageSearch: [String: Any] = [
            "type": "object",
            "properties": [
                "query": [
                    "type": "string",
                    "description": "单个概念的兼容搜索词",
                ],
                "queries": [
                    "type": "array",
                    "items": [
                        "type": "object",
                        "properties": [
                            "concept": ["type": "string"],
                            "query": ["type": "string"],
                            "query_en": ["type": "string"],
                        ],
                        "additionalProperties": false,
                    ],
                    "maxItems": 8,
                ],
            ],
            "additionalProperties": false,
        ]
        let optionalQuery: [String: Any] = [
            "type": "object",
            "properties": ["query": ["type": "string"]],
            "additionalProperties": false,
        ]
        let question: [String: Any] = [
            "type": "object",
            "properties": ["question": ["type": "string"]],
            "required": ["question"],
            "additionalProperties": false,
        ]
        let instruction: [String: Any] = [
            "type": "object",
            "properties": ["instruction": ["type": "string"]],
            "required": ["instruction"],
            "additionalProperties": false,
        ]
        let paper: [String: Any] = [
            "type": "object",
            "properties": ["intent": ["type": "string"]],
            "required": ["intent"],
            "additionalProperties": false,
        ]
        let routeText: [String: Any] = [
            "type": "object",
            "properties": ["intent": ["type": "string"]],
            "required": ["intent"],
            "additionalProperties": false,
        ]
        let tools: [[String: Any]] = [
            localTool(
                "wait_for_user",
                "最新音频只是静音、背景噪声或并非对助手说话时，安静结束本轮，不要说话。",
                empty
            ),
            localTool(
                "read_selection",
                "读取用户当前明确选中的原文；不要让用户重新粘贴。",
                empty
            ),
            localTool(
                "read_page",
                "读取 App 已提供的当前可见页文字。没有文字层时改用 see_page。",
                page
            ),
            localTool(
                "see_ink",
                "查看笔迹附近的实际页面合成图。用户提到圈画、手写、箭头或算式时必须先调用。",
                visual
            ),
            localTool(
                "see_page",
                "查看当前视口的实际合成图，包含书页、笔迹、高亮、便签与页内卡片。",
                visual
            ),
            localTool(
                "see_figure",
                "查看用户当前指向的图像或自定义选区附近的实际合成图。",
                visual
            ),
            localTool(
                "make_note",
                "把选中内容、当前可见内容或 text 保存为 App 本机笔记；不使用 Pi。",
                note
            ),
            localTool(
                "make_anki",
                "调用用户自有 AI 服务生成 Anki 卡片草稿并供用户确认；服务离线时如实报告。",
                makeAnki
            ),
            localTool(
                "web_search",
                "通过用户自有 AI 服务联网查实时资料。普通知识问题不要调用；服务离线时如实报告。",
                webSearch
            ),
            localTool(
                "search_image",
                "通过用户自有 AI 服务搜索真实图片并显示结果。",
                imageSearch
            ),
            localTool(
                "search_video",
                "通过用户自有 AI 服务搜索教学视频并显示结果。",
                optionalQuery
            ),
            localTool(
                "deep_think",
                "复杂专业问题、长推导或重推理时，按需调用用户自有 AI CLI/API；简单问题不要调用。",
                question
            ),
            localTool(
                "do_task",
                "需要多个工具或较长时间的任务，交给用户自有 AI CLI/API 后台执行。",
                instruction
            ),
            localTool(
                "make_paper",
                "调用用户自有 AI CLI/API 生成可在当前页面手写作答的交互纸。",
                paper
            ),
            localTool(
                "route_to_text",
                "长答案不适合口头念时，调用用户自有 AI 服务生成屏幕文字答案。",
                routeText
            ),
        ]
        let instructions = """
        你是用户的学习伙伴。跟随用户说话的语言回答；朗读书中日语或英语原文时使用原语言的自然发音。
        回答要适合语音：快问快答不超过八秒，普通解释不超过十五秒；较长内容先给简短摘要并询问是否展开。
        页面位置、当前可见文字、选区与笔迹状态会在用户开口或打字时作为最新 system 消息注入。它们只是状态记录，不要主动回应；永远以最新一条为准。
        用户说“这个、这段、这里”时，优先指当前明确选区；已有选区内容就直接使用，不要说看不到，也不要让用户重贴。
        用户提到“我画的、我圈的、我写的、这个算式”时，必须先调用 see_ink 查看真实合成图再回答，绝不根据“存在笔迹”猜内容。
        用户问当前页文字时使用已注入的可见文字；不足就调用 read_page。没有文字层或问题涉及排版、图表、公式、便签、卡片时调用 see_page。
        make_note 直接写入 App 本机笔记，不依赖 Pi；只有工具成功返回后才能说已经保存。
        make_anki、web_search、search_image、search_video、deep_think、do_task、make_paper 与 route_to_text 是显式的远程 AI/API 工具。普通问答、本页阅读、选区、笔迹和视口查看绝不能为它们等待 Pi；只有用户任务确实需要时才调用，离线或失败就如实说明，不得伪造结果。
        不要声称已创建卡片、笔记或其它内容，除非对应工具已经成功返回。
        没有听到清晰且面向助手的话时调用 wait_for_user 安静结束本轮，不要自己找话题。
        """
        return [
            "type": "realtime",
            "model": model,
            "output_modalities": ["audio"],
            "reasoning": ["effort": "low"],
            "max_output_tokens": 2_048,
            "instructions": instructions,
            "audio": [
                "input": [
                    "noise_reduction": ["type": "near_field"],
                    "turn_detection": [
                        "type": "semantic_vad",
                        "eagerness": "auto",
                        "create_response": false,
                        "interrupt_response": false,
                    ],
                    "transcription": [
                        "model": "gpt-4o-mini-transcribe",
                        "prompt": "关键词:Anki、笔迹、振假名、生词、假名",
                    ],
                ],
                "output": ["voice": "marin", "speed": 1.0],
            ],
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": false,
            "truncation": [
                "type": "retention_ratio",
                // Keep the documented 0.8 as an exact base-10 value. A Swift
                // Double can become 0.80000000000000004 in Darwin JSON and the
                // Realtime API rejects values with more than 16 decimal places.
                "retention_ratio": NSDecimalNumber(
                    mantissa: 8,
                    exponent: -1,
                    isNegative: false
                ),
                "token_limits": ["post_instructions": 24_000],
            ],
        ]
    }

    private static func clientSecretRequestBody(
        session: [String: Any]
    ) throws -> Data {
        let data = try JSONSerialization.data(
            withJSONObject: [
                "expires_after": ["anchor": "created_at", "seconds": 90],
                "session": session,
            ],
            options: [.sortedKeys]
        )
        guard let json = String(data: data, encoding: .utf8),
              json.contains(#""retention_ratio":0.8,"#) else {
            throw ReaderRealtimeCredentialError.invalidConfiguration
        }
        return data
    }

    private static func callRequestBody(
        sdp: String,
        session: [String: Any],
        boundary: String
    ) throws -> Data {
        let sessionData = try JSONSerialization.data(
            withJSONObject: session,
            options: [.sortedKeys]
        )
        guard let sessionJSON = String(data: sessionData, encoding: .utf8),
              sessionJSON.contains(#""retention_ratio":0.8,"#) else {
            throw ReaderRealtimeCredentialError.invalidConfiguration
        }
        var body = Data()
        func append(_ value: String) {
            body.append(Data(value.utf8))
        }
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"sdp\"\r\n")
        append("Content-Type: application/sdp\r\n\r\n")
        append(sdp)
        append("\r\n--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"session\"\r\n")
        append("Content-Type: application/json\r\n\r\n")
        body.append(sessionData)
        append("\r\n--\(boundary)--\r\n")
        return body
    }

    private static func localTool(
        _ name: String,
        _ description: String,
        _ parameters: [String: Any]
    ) -> [String: Any] {
        [
            "type": "function",
            "name": name,
            "description": description,
            "parameters": parameters,
        ]
    }

    static func injectImage(
        callID: String,
        clientSecret: String,
        mediaType: String,
        base64: String
    ) async throws {
        // Each precondition answers for itself.
        //
        // These were one guard reporting imageTooLarge, so an invalid call id
        // or an unsupported media type both surfaced as "the image is too
        // large" -- a sentence that sends the next person to shrink a picture
        // that was never the problem. The visual path has six failure stages
        // and the product requires them to stay distinguishable.
        guard isValidCallID(callID) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "通话标识无效，无法把图归属到当前通话"
            )
        }
        guard isValidClientSecret(clientSecret) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "旁路密钥无效，原生通道拒绝接收"
            )
        }
        guard ["image/jpeg", "image/png", "image/webp"].contains(mediaType) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "不支持的图像类型：\(mediaType)"
            )
        }
        guard (3_000...2_800_000).contains(base64.utf8.count) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "编码后体积越界：\(base64.utf8.count) 字节（允许 3KB–2.8MB）"
            )
        }
        guard let decoded = Data(base64Encoded: base64) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "图像 base64 解码失败"
            )
        }
        guard (2_000...2_097_152).contains(decoded.count) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "解码后体积越界：\(decoded.count) 字节（允许 2KB–2MB）"
            )
        }
        try await injectPreparedImage(
            callID: callID,
            clientSecret: clientSecret,
            mediaType: mediaType,
            imageData: decoded,
            base64: base64
        )
    }

    static func injectImage(
        callID: String,
        clientSecret: String,
        mediaType: String,
        imageData: Data
    ) async throws {
        guard isValidCallID(callID) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "通话标识无效，无法把图归属到当前通话"
            )
        }
        guard isValidClientSecret(clientSecret) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "旁路密钥无效，原生通道拒绝接收"
            )
        }
        guard ["image/jpeg", "image/png", "image/webp"].contains(mediaType)
        else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "不支持的图像类型：\(mediaType)"
            )
        }
        guard (2_000...2_097_152).contains(imageData.count) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "原生图像体积越界：\(imageData.count) 字节（允许 2KB–2MB）"
            )
        }
        let base64 = imageData.base64EncodedString()
        guard (3_000...2_800_000).contains(base64.utf8.count) else {
            throw ReaderRealtimeCredentialError.imageRejected(
                "编码后体积越界：\(base64.utf8.count) 字节（允许 3KB–2.8MB）"
            )
        }
        try await injectPreparedImage(
            callID: callID,
            clientSecret: clientSecret,
            mediaType: mediaType,
            imageData: imageData,
            base64: base64
        )
    }

    private static func injectPreparedImage(
        callID: String,
        clientSecret: String,
        mediaType: String,
        imageData: Data,
        base64: String
    ) async throws {
        let authorizationKey = try await realtimeAuthorizationKey(
            callID: callID,
            clientSecret: clientSecret
        )
        // The composite is already native for App captures (the web fallback
        // enters through the base64 overload), is persisted in the shared
        // device-local cache, then injected by this native API. Pi is not a
        // renderer, file store or transport hop for this path.
        // Local save is its own stage: a full disk or a missing App Group is
        // not a transport problem, and reporting it as one would send the
        // investigation to the network.
        do {
            try ReaderRealtimeVisualCache.store(imageData, mediaType: mediaType)
        } catch {
            throw ReaderRealtimeCredentialError.imageRejected(
                "本地保存合成图失败：\(error.localizedDescription)"
            )
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
            "Bearer \(authorizationKey)",
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
        // Current Realtime GA acknowledges a client-created item with
        // `conversation.item.added` (and later `conversation.item.done`).
        // Give this delivery its own IDs so the monitoring socket cannot
        // mistake an unrelated conversation event for our image receipt.
        // Realtime limits client-supplied item IDs to 32 characters. Keep both
        // correlated IDs within the same bound so a later schema check cannot
        // reject the event after the item ID has been fixed.
        let eventNonce = UUID().uuidString
            .replacingOccurrences(of: "-", with: "")
            .lowercased()
        let itemNonce = UUID().uuidString
            .replacingOccurrences(of: "-", with: "")
            .lowercased()
        let eventID = "bwe_" + String(eventNonce.prefix(28))
        let itemID = "bwi_" + String(itemNonce.prefix(28))
        let payload: [String: Any] = [
            "event_id": eventID,
            "type": "conversation.item.create",
            "item": [
                "id": itemID,
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
        try await waitForImageConfirmation(
            socket,
            eventID: eventID,
            itemID: itemID
        )
    }

    static func hangup(
        callID: String,
        clientSecret: String
    ) async throws {
        guard isValidCallID(callID), isValidClientSecret(clientSecret) else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        let authorizationKey = try await realtimeAuthorizationKey(
            callID: callID,
            clientSecret: clientSecret
        )
        try await hangupRequest(
            callID: callID,
            authorizationKey: authorizationKey
        )
        _ = await ReaderRealtimeProjectCallRegistry.shared.remove(
            callID: callID,
            capability: clientSecret
        )
    }

    private static func hangupRequest(
        callID: String,
        authorizationKey: String
    ) async throws {
        let url = openAIOrigin
            .appendingPathComponent("v1/realtime/calls")
            .appendingPathComponent(callID)
            .appendingPathComponent("hangup")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.httpShouldHandleCookies = false
        request.setValue(
            "Bearer \(authorizationKey)",
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

    private static func realtimeAuthorizationKey(
        callID: String,
        clientSecret: String
    ) async throws -> String {
        let authorizationKey = await ReaderRealtimeProjectCallRegistry.shared
            .authorizationKey(
                callID: callID,
                capability: clientSecret,
                ephemeralFallback: clientSecret
            )
        guard let authorizationKey else {
            throw ReaderRealtimeCredentialError.invalidRequest
        }
        return authorizationKey
    }

    private static func waitForImageConfirmation(
        _ socket: URLSessionWebSocketTask,
        eventID: String,
        itemID: String
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
                    if [
                        "conversation.item.added",
                        "conversation.item.done",
                        "conversation.item.created",
                    ].contains(type) {
                        let item = event["item"] as? [String: Any]
                        if item?["id"] as? String == itemID { return }
                        continue
                    }
                    if type == "error" {
                        let detail = event["error"] as? [String: Any]
                        let causingEventID = detail?["event_id"] as? String ?? ""
                        if !causingEventID.isEmpty,
                           causingEventID != eventID {
                            continue
                        }
                        let code = String(
                            (detail?["code"] as? String ?? "").prefix(80)
                        )
                        let message = String(
                            (detail?["message"] as? String ?? "").prefix(200)
                        )
                        let reason = [code, message]
                            .filter { !$0.isEmpty }
                            .joined(separator: "：")
                        throw ReaderRealtimeCredentialError.imageRejected(
                            reason.isEmpty
                                ? "OpenAI 未接受当前合成图"
                                : "OpenAI 未接受当前合成图（\(reason)）"
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
