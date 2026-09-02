import Foundation
import WebKit

/// 手机侧：把手表语音桥的 token 从 Pi 取来，送进手表。
///
/// ## 为什么这一步必须由手机做
///
/// 手表上没有键盘也没有登录界面。而手机本来就持有 Pi 的会话 cookie
/// （用户在 App 里登录过），所以复用它，不必再造一套配对流程。
///
/// ## ⚠ 这是一次性配给，不是每次通话的依赖
///
/// 配好之后 token 存在手表的 Keychain 里，通话时手表**直接打 Pi**，
/// 不再经过手机。这跟卡片/待办那条「手表是手机的镜子」的链路是两回事：
/// 那条永远依赖手机开着，这条只在第一次配给时需要。
///
/// 说清这一点是因为用户最初的抱怨正是「完全依赖手机 app 是否打开」——
/// 如果这里含糊，看代码的人会以为语音也有同样的毛病。
@MainActor
enum ReaderWatchTokenProvisioning {
    /// 与 `ReaderWatchVoiceTurn.piOrigin` 同源。刻意各写一份而不互相引用：
    /// 那个类型是 WebView 的消息处理器，为了一个常量把生命周期绑上去不划算。
    static let piOrigin = "https://bwicarus-2.taile44d0c.ts.net"

    enum Failure: LocalizedError {
        case notSignedIn
        case serverSaid(String)
        case malformed

        var errorDescription: String? {
            switch self {
            case .notSignedIn:
                // 手表上没有登录界面，所以这条必须说清该去哪儿修。
                return "手机还没登录 Pi，先在 App 里登录一次"
            case .serverSaid(let why): return why
            case .malformed: return "Pi 返回的内容不是预期格式"
            }
        }
    }

    /// 取 token。**只取，不负责送** —— 送去手表由 `ReaderWatchLink` 做，
    /// 两件事分开是为了让"取失败"和"送失败"在日志里分得开。
    static func fetchToken() async throws -> String {
        let cookies = await WKWebsiteDataStore.default().httpCookieStore.allCookies()
            .filter { $0.domain.contains("bwicarus") }
        guard !cookies.isEmpty else { throw Failure.notSignedIn }

        var request = URLRequest(url: URL(string: piOrigin + "/api/voice/watch-token")!)
        for (key, value) in HTTPCookie.requestHeaderFields(with: cookies) {
            request.setValue(value, forHTTPHeaderField: key)
        }
        request.timeoutInterval = 15
        // 这是一把凭证，任何一层都不许留副本。
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData

        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            if http.statusCode == 401 || http.statusCode == 403 {
                throw Failure.notSignedIn
            }
            // 503 是「Pi 上还没配」，跟「网络坏了」完全是两件事，
            // 所以把服务端那句中文原样透出来，别折成一个笼统的失败。
            let said = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])
                .flatMap { $0?["error"] as? String }
            throw Failure.serverSaid(said ?? "Pi 返回 HTTP \(http.statusCode)")
        }
        guard let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              payload["ok"] as? Bool == true,
              let token = payload["token"] as? String,
              token.count >= 32
        else { throw Failure.malformed }
        return token
    }
}
