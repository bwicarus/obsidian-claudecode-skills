import AuthenticationServices
import CryptoKit
import Combine
import Foundation
import WebKit

private final class ReaderAppleNoRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

@MainActor
final class ReaderAppleSignInModel: ObservableObject {
    @Published private(set) var busy = false
    @Published private(set) var ready = false
    @Published private(set) var linking = false
    @Published private(set) var needsLink = false
    @Published private(set) var signedIn = false
    @Published private(set) var username = ""
    @Published private(set) var error: String?
    private let dataStore: WKWebsiteDataStore
    private let network: URLSession
    private var nonce = "", state = "", ticket = ""

    init(dataStore: WKWebsiteDataStore) {
        self.dataStore = dataStore
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false
        config.httpCookieStorage = nil
        config.timeoutIntervalForRequest = 20
        network = URLSession(configuration: config, delegate: ReaderAppleNoRedirect(), delegateQueue: nil)
    }

    func prepare() async {
        guard !busy, !signedIn else { return }
        busy = true; ready = false; error = nil; needsLink = false; ticket = ""
        defer { busy = false }
        do {
            let result = try await send("challenge", body: [:])
            guard let nonce = result["nonce"] as? String, let state = result["state"] as? String else { throw failure("登录信息不完整，请重试。") }
            self.nonce = nonce; self.state = state
            linking = result["linking"] as? Bool == true
            username = result["username"] as? String ?? ""
            signedIn = linking && result["apple_linked"] as? Bool == true
            ready = true
        } catch { self.error = error.localizedDescription }
    }

    func configure(_ request: ASAuthorizationAppleIDRequest) {
        request.requestedScopes = [.fullName, .email]
        request.nonce = SHA256.hash(data: Data(nonce.utf8)).map { String(format: "%02x", $0) }.joined()
        request.state = state
    }

    func complete(_ result: Result<ASAuthorization, Error>) async {
        guard !busy else { return }
        busy = true; error = nil
        defer { busy = false }
        do {
            let authorization = try result.get()
            guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  credential.state == state, let data = credential.identityToken,
                  let token = String(data: data, encoding: .utf8) else { throw failure("未收到有效的 Apple 登录凭据。") }
            let receipt = try await send("complete", body: ["state": state, "identity_token": token])
            ready = false
            if receipt["link_required"] as? Bool == true, let ticket = receipt["ticket"] as? String {
                self.ticket = ticket; needsLink = true
            } else { try await finish(receipt) }
        } catch {
            if (error as? ASAuthorizationError)?.code != .canceled { self.error = error.localizedDescription }
        }
    }

    func link(username: String, password: String, invite: String) async {
        guard !busy, needsLink else { return }
        busy = true; error = nil
        defer { busy = false }
        do {
            let result = try await send("link", body: ["ticket": ticket, "username": username, "password": password, "invite": invite])
            try await finish(result)
        } catch { self.error = error.localizedDescription }
    }

    func signOut() async {
        guard !busy else { return }
        busy = true; error = nil
        do {
            _ = try await send("logout", body: [:])
            signedIn = false; linking = false; needsLink = false; ready = false
            username = ""; ticket = ""; nonce = ""; state = ""
            try ReaderAccountTokenStore.shared.clear()
            busy = false
            await prepare()
        } catch { busy = false; self.error = error.localizedDescription }
    }

    private func finish(_ receipt: [String: Any]) async throws {
        guard let username = receipt["username"] as? String, !username.isEmpty else { throw failure("登录结果不完整，请重试。") }
        self.username = username; signedIn = true; needsLink = false; ticket = ""
        await ReaderAccountTokenProvisioner.shared.ensureToken(dataStore: dataStore, reason: "apple-login", forceRefresh: true)
    }

    private func send(_ action: String, body: [String: Any]) async throws -> [String: Any] {
        let url = ReaderAccountTokenProvisioner.origin.appendingPathComponent("login/apple/" + action)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let cookies: [HTTPCookie] = await withCheckedContinuation { continuation in
            dataStore.httpCookieStore.getAllCookies { continuation.resume(returning: $0) }
        }
        let matching = cookies.filter { cookie in
            let domain = cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: "."))
            return domain == url.host && url.path.hasPrefix(cookie.path)
        }
        for (key, value) in HTTPCookie.requestHeaderFields(with: matching) { request.setValue(value, forHTTPHeaderField: key) }
        let (data, response) = try await network.data(for: request)
        guard let response = response as? HTTPURLResponse,
              response.url?.host == url.host, data.count < 100_000 else { throw failure("登录服务器响应无效。") }
        var headers: [String: String] = [:]
        for (key, value) in response.allHeaderFields { headers[String(describing: key)] = String(describing: value) }
        for cookie in HTTPCookie.cookies(withResponseHeaderFields: headers, for: url) {
            guard cookie.domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")) == url.host else { continue }
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                dataStore.httpCookieStore.setCookie(cookie) { continuation.resume() }
            }
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw failure("服务器尚未提供 Apple 登录，请稍后重试。")
        }
        guard response.statusCode == 200, object["ok"] as? Bool == true else {
            throw failure(object["error"] as? String ?? "登录失败，请重试。")
        }
        return object
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "ReaderAppleSignIn", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
