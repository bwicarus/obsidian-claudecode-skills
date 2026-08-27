import Foundation
import WebKit

/// 手表「按住说话」的手机端：把手表录来的一段音频跑完一轮问答。
///
/// ## 为什么是「按住说话」而不是像打电话那样连续对话
///
/// 手表接不上 Windows 那条语音桥，原因有两层，**第二层才是决定性的**：
///
/// 1. 那条桥是 48kHz/mono/s16le、20ms 定长帧、序号严格连续的**纯双工电话**
///    （`DirectVoiceProtocol.swift:20-25`）。协议里根本没有「轮次」这个概念 ——
///    对端是 PC 上桌面 app 的语音模式，桥只是它的虚拟耳机，说完没说完由那个
///    桌面 app 自己判。一来一回的形态接不上去。
/// 2. 就算能接：**watchOS 禁用 `URLSessionWebSocketTask`**（Apple TN3135 把
///    整个 Network framework 归为低层网络，三个豁免场景外一律不给）。所以
///    「把 Windows 桥暴露到公网让手表直连」这条路买不到任何东西 —— 手表压根
///    开不了 WebSocket。这一条跟安全取舍无关，是纯技术死路。
///
/// ## 走的是哪条
///
/// Pi 上早就有一条回合制链路在线上跑，网页版语音（`static/voice.js`）就是它
/// 现役的消费者：
///
///     POST /api/voice/transcribe   multipart audio → {ok, text}
///     POST /api/voice/agent        {transcript} → {ok, speak, client_actions}
///
/// **服务端一行都不用改。** 手表录一段 → 手机转发 → 念回结果。
///
/// ⚠ 带宽算术（说明为什么不是"调优一下就能连续对话"）：那条双工桥每方向
/// 97.8 KB/s，而 WCSession 最快的 `sendMessage` 单条上限 64KB 且是串行的
/// 电源管理 IPC 队列，没有流式原语。要扛那个流得每秒 50 条消息 ——
/// 这不是参数问题，是**传输类型不对**。
@MainActor
enum ReaderWatchVoiceTurn {
    /// Pi 的基址。与 ReaderNativePiGateway.piHost 同源，改一处要改两处 ——
    /// 但故意不去引用它：那个类是 WebView 的消息处理器，为了一个常量把
    /// 生命周期绑上去不划算。
    static let piOrigin = "https://bwicarus.taile44d0c.ts.net"

    struct Outcome {
        let transcript: String
        let reply: String
    }

    enum Failure: LocalizedError {
        case notSignedIn
        case emptyAudio
        case transcribeFailed(String)
        case heardNothing
        case agentFailed(String)

        var errorDescription: String? {
            switch self {
            case .notSignedIn:
                // 手表上没有登录界面，所以这条必须说清该去哪儿修。
                return "手机还没登录 Pi，先在 App 里登录一次"
            case .emptyAudio: return "没录到声音"
            case .transcribeFailed(let why): return "转写失败：" + why
            case .heardNothing: return "没听清，再说一次"
            case .agentFailed(let why): return "助手没回应：" + why
            }
        }
    }

    /// Pi 的会话 cookie。
    ///
    /// ⚠ 直接读**进程级**的 `WKWebsiteDataStore.default()`，不经过 WebView ——
    /// 手机被 WatchConnectivity 从后台唤醒时根本没有 WebView 实例，
    /// 从视图层取 cookie 那条路在那个时刻是空的。
    /// （ReaderWebView.swift:397 确认用的就是 .default()，所以这里读得到。）
    static func piCookies() async -> [HTTPCookie] {
        let all = await WKWebsiteDataStore.default().httpCookieStore.allCookies()
        return all.filter { $0.domain.contains("bwicarus") }
    }

    /// 跑完一轮：音频 → 文字 → 回答。
    ///
    /// - Parameter clip: 手表录的 AAC/m4a。
    ///
    ///   ⚠ 为什么不是 WAV：16kHz mono s16 是 32000 B/s，而 WCSession 单条
    ///   上限 64KB —— 只够 **2 秒**。AAC 同采样率约 3000 B/s，能录 20 秒。
    ///   代价是 Pi 侧多一次 ffmpeg 转 FLAC（`/api/voice/transcribe` 本来就
    ///   有这条分支），短片段很划算。
    static func run(clip: Data, cookies: [HTTPCookie]) async throws -> Outcome {
        guard !clip.isEmpty else { throw Failure.emptyAudio }
        let piCookies = cookies.filter { $0.domain.contains("bwicarus") }
        guard !piCookies.isEmpty else { throw Failure.notSignedIn }

        let transcript = try await transcribe(clip: clip, cookies: piCookies)
        guard !transcript.isEmpty else { throw Failure.heardNothing }
        let reply = try await ask(transcript: transcript, cookies: piCookies)
        return Outcome(transcript: transcript, reply: reply)
    }

    // ── 一、转写 ──

    private static func transcribe(
        clip: Data, cookies: [HTTPCookie]
    ) async throws -> String {
        let boundary = "bw-watch-" + UUID().uuidString
        var body = Data()
        body.append(("--" + boundary + "\r\n").data(using: .utf8)!)
        body.append(("Content-Disposition: form-data; name=\"audio\"; "
                     + "filename=\"watch.m4a\"\r\n").data(using: .utf8)!)
        body.append("Content-Type: audio/mp4\r\n\r\n".data(using: .utf8)!)
        body.append(clip)
        body.append(("\r\n--" + boundary + "--\r\n").data(using: .utf8)!)

        var request = URLRequest(url: URL(string: piOrigin + "/api/voice/transcribe")!)
        request.httpMethod = "POST"
        request.setValue(
            "multipart/form-data; boundary=" + boundary,
            forHTTPHeaderField: "Content-Type")
        apply(cookies, to: &request)
        request.httpBody = body
        // STT 是批式的一次 POST。给足时间但要有上限 —— 手表上转圈转到天荒地老
        // 比失败更糟，用户不知道该不该再按一次。
        request.timeoutInterval = 45

        let payload = try await json(request, failure: Failure.transcribeFailed)
        guard payload["ok"] as? Bool == true else {
            throw Failure.transcribeFailed(
                (payload["error"] as? String) ?? "服务端没说原因")
        }
        return ((payload["text"] as? String) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // ── 二、问答 ──

    private static func ask(
        transcript: String, cookies: [HTTPCookie]
    ) async throws -> String {
        var request = URLRequest(url: URL(string: piOrigin + "/api/voice/agent")!)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        apply(cookies, to: &request)
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "transcript": transcript,
            // 上下文留空：手表上没有"当前页"这种东西，编一个反而会让助手
            // 按错误的前提回答。
            "context": [String: Any](),
        ])
        request.timeoutInterval = 60

        let payload = try await json(request, failure: Failure.agentFailed)
        guard payload["ok"] as? Bool == true else {
            throw Failure.agentFailed(
                (payload["error"] as? String) ?? "服务端没说原因")
        }
        let speak = ((payload["speak"] as? String) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        // 助手可能只回了 client_actions 没回话。那种情况下手表上要有话说,
        // 否则用户面对一片空白不知道成没成。
        return speak.isEmpty ? "已处理" : speak
    }

    // ── 公用 ──

    private static func apply(_ cookies: [HTTPCookie], to request: inout URLRequest) {
        let header = HTTPCookie.requestHeaderFields(with: cookies)
        for (key, value) in header {
            request.setValue(value, forHTTPHeaderField: key)
        }
    }

    private static func json(
        _ request: URLRequest,
        failure: (String) -> Failure
    ) async throws -> [String: Any] {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw failure(error.localizedDescription)
        }
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            // 401/403 几乎一定是 Pi 会话过期。把它翻译成"去哪儿修"而不是
            // 把状态码丢给手表 —— 表上那行字是用户唯一能看到的东西。
            if http.statusCode == 401 || http.statusCode == 403 {
                throw Failure.notSignedIn
            }
            throw failure("HTTP \(http.statusCode)")
        }
        guard let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any]
        else {
            throw failure("响应不是 JSON")
        }
        return payload
    }
}
