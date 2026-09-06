import Foundation
import WebKit
import os

/// App 登录服务器之后，替 Safari 扩展铸一枚设备令牌并存进共享 Keychain。
///
/// 用户 2026-09-06："就不能设计为 App 登录后扩展自动登录么"。触发点两处：
/// 登录页跳出登录流程那一刻（ReaderPiLoginView），以及 App 启动（早就登录过的老用户）。
///
/// - 幂等：Keychain 里已有同一服务器的令牌就不再铸。服务端 api_tokens 表里的令牌不随
///   会话过期，一枚够用很久；每次启动都铸只会在服务器上堆一排令牌。
/// - 凭据来源是 App 的网站数据存储里的服务器会话 cookie（登录页写进去的那份），
///   跟 ReaderNativePiGateway 给同步请求带 cookie 是同一个来源；没有 cookie 就是没登录，
///   安静返回，不弹窗 —— 这不是用户发起的动作，弹出来只会让人以为登录失败。
/// - 令牌只进 Keychain。扩展经 native messaging 取走后走它自己的校验与保存路径。
@MainActor
final class ReaderAccountTokenProvisioner {
    static let shared = ReaderAccountTokenProvisioner()

    /// 与 ReaderNativePiSyncBridge.loginURL 同一台服务器（Windows 桥）。
    static let origin = URL(string: "https://bwicarus-2.taile44d0c.ts.net")!

    private let log = Logger(
        subsystem: "space.bwicarus.bwreader2",
        category: "account-token"
    )
    private var inFlight = false

    private init() {}

    func ensureToken(dataStore: WKWebsiteDataStore, reason: String) async {
        guard !inFlight else { return }
        inFlight = true
        defer { inFlight = false }

        do {
            if let existing = try ReaderAccountTokenStore.shared.loadIfPresent(),
               existing.origin == Self.origin.absoluteString {
                log.debug("device token already present (reason=\(reason, privacy: .public))")
                return
            }
        } catch {
            // 读不出来（损坏 / 旧格式）就当没有：下面会重铸并覆盖。
            log.error("keychain read failed, re-minting: \(error.localizedDescription, privacy: .public)")
        }

        let cookies = await Self.sessionCookies(for: Self.origin, in: dataStore)
        guard !cookies.isEmpty else {
            log.info("no server session cookie; App not logged in (reason=\(reason, privacy: .public))")
            return
        }
        do {
            let label = "BWReader iPad Safari 扩展 · " + Self.dateLabel()
            let token = try await Self.mint(cookies: cookies, label: label)
            try ReaderAccountTokenStore.shared.save(
                origin: Self.origin.absoluteString,
                token: token,
                label: label
            )
            log.notice("device token minted for the Safari extension (reason=\(reason, privacy: .public))")
        } catch ReaderAccountTokenError.notLoggedIn {
            log.info("server session expired; token not minted (reason=\(reason, privacy: .public))")
        } catch {
            log.error("minting device token failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    // MARK: - pieces

    private static func sessionCookies(
        for origin: URL,
        in dataStore: WKWebsiteDataStore
    ) async -> [HTTPCookie] {
        let host = (origin.host ?? "").lowercased()
        let all: [HTTPCookie] = await withCheckedContinuation { continuation in
            dataStore.httpCookieStore.getAllCookies { continuation.resume(returning: $0) }
        }
        return all.filter { cookie in
            let domain = cookie.domain.lowercased()
                .trimmingCharacters(in: CharacterSet(charactersIn: "."))
            guard domain == host || host.hasSuffix("." + domain) else { return false }
            if let expires = cookie.expiresDate, expires < Date() { return false }
            return true
        }
    }

    private static func mint(cookies: [HTTPCookie], label: String) async throws -> String {
        var request = URLRequest(url: origin.appendingPathComponent("api/tokens"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.httpShouldHandleCookies = false
        HTTPCookie.requestHeaderFields(with: cookies).forEach {
            request.setValue($1, forHTTPHeaderField: $0)
        }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["label": label])

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw ReaderAccountTokenError.mintFailed("no HTTP response")
        }
        // 会话过期时服务端 302 到 /login，URLSession 跟过去拿到的是登录页 HTML：
        // 按"没登录"处理，不当成服务器坏了。
        let mime = (http.mimeType ?? "").lowercased()
        if http.url?.path == "/login" || !mime.contains("application/json") {
            throw ReaderAccountTokenError.notLoggedIn
        }
        guard http.statusCode == 200 else {
            throw ReaderAccountTokenError.mintFailed("HTTP \(http.statusCode)")
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["ok"] as? Bool == true,
              let token = object["token"] as? String,
              ReaderAccountTokenStore.isValidToken(token)
        else {
            throw ReaderAccountTokenError.invalidToken
        }
        return token
    }

    private static func dateLabel() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date())
    }
}
