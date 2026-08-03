import SwiftUI
import UIKit
import WebKit

private let readerStartURL = URL(string: "https://bwicarus.taile44d0c.ts.net/pdf/")!
private let nativeComputerVoiceMessageName = "bwNativeComputerVoice"
private let nativeComputerContextMessageName = "bwNativeComputerContext"

private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: WKScriptMessageHandler?

    init(delegate: WKScriptMessageHandler) {
        self.delegate = delegate
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        delegate?.userContentController(
            userContentController,
            didReceive: message
        )
    }
}

@MainActor
final class ReaderWebViewModel: NSObject, ObservableObject {
    enum NativeVoiceHandoffError: LocalizedError {
        case webVoiceAlreadyActive
        case contextRelayUnavailable

        var errorDescription: String? {
            switch self {
            case .webVoiceAlreadyActive:
                return "网页电脑语音仍在通话，请先结束当前电脑语音"
            case .contextRelayUnavailable:
                return "Reader 原生上下文接力尚未准备好"
            }
        }
    }

    let webView: WKWebView

    @Published private(set) var isLoading = false
    @Published private(set) var loadError: String?
    private var nativeComputerVoiceMessageProxy: WeakScriptMessageHandler?
    private var nativeComputerContextMessageProxy: WeakScriptMessageHandler?
    private weak var nativeVoiceBridge: NativeVoiceBridge?

    override init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []

        let preferences = WKWebpagePreferences()
        preferences.allowsContentJavaScript = true
        configuration.defaultWebpagePreferences = preferences

        webView = WKWebView(
            frame: .zero,
            configuration: configuration
        )
        super.init()

        let contentController = webView.configuration.userContentController
        let nativeComputerVoiceMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativeComputerVoiceMessageProxy =
            nativeComputerVoiceMessageProxy
        contentController.add(
            nativeComputerVoiceMessageProxy,
            name: nativeComputerVoiceMessageName
        )
        let nativeComputerContextMessageProxy =
            WeakScriptMessageHandler(delegate: self)
        self.nativeComputerContextMessageProxy =
            nativeComputerContextMessageProxy
        contentController.add(
            nativeComputerContextMessageProxy,
            name: nativeComputerContextMessageName
        )
        contentController.addUserScript(WKUserScript(
            source: """
            (() => {
              window.__BW_NATIVE_COMPUTER_VOICE__ = true;
              window.__BW_NATIVE_COMPUTER_VOICE_APP_VERSION__ =
                "\(nativeAppBuildVersion)";

              const selector = "#asst-computer, #vc-top-computer";
              let latest = {
                active: false,
                busy: false,
                sessionId: null,
                title: "电脑语音未连接"
              };
              const applyButton = (button) => {
                button.classList.toggle("on", latest.active === true);
                button.classList.toggle(
                  "connecting",
                  latest.busy === true && latest.active !== true
                );
                button.classList.remove("speaking");
                button.title = latest.title;
                button.setAttribute("aria-label", latest.title);
                button.setAttribute(
                  "aria-pressed",
                  latest.active === true ? "true" : "false"
                );
                button.setAttribute(
                  "aria-busy",
                  latest.busy === true ? "true" : "false"
                );
                button.disabled = latest.busy === true;
              };
              const applyAll = () => {
                document.querySelectorAll(selector).forEach(applyButton);
              };
              window.__bwNativeComputerVoiceApplyState = (value) => {
                const state = value && typeof value === "object" ? value : {};
                latest = {
                  active: state.active === true,
                  busy: state.busy === true,
                  sessionId: typeof state.sessionId === "string"
                    ? state.sessionId
                    : null,
                  title: String(state.title || "电脑语音未连接")
                };
                window.__BW_NATIVE_COMPUTER_VOICE_STATE__ = {
                  active: latest.active,
                  busy: latest.busy,
                  sessionId: latest.sessionId
                };
                window.dispatchEvent(new CustomEvent(
                  "bw-native-computer-voice-state",
                  { detail: window.__BW_NATIVE_COMPUTER_VOICE_STATE__ }
                ));
                applyAll();
              };

              new MutationObserver((records) => {
                const addedButton = records.some((record) =>
                  Array.from(record.addedNodes || []).some((node) =>
                    node?.nodeType === 1 && (
                      node.matches?.(selector) ||
                      node.querySelector?.(selector)
                    )
                  )
                );
                if (addedButton) applyAll();
              }).observe(document, { childList: true, subtree: true });
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))

        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsLinkPreview = false
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.contentInsetAdjustmentBehavior = .never
    }

    func loadIfNeeded() {
        guard webView.url == nil else {
            return
        }
        reload()
    }

    func bind(nativeVoiceBridge: NativeVoiceBridge) {
        self.nativeVoiceBridge = nativeVoiceBridge
        updateNativeVoiceButton(state: nativeVoiceBridge.state)
    }

    func reload() {
        loadError = nil
        let request = URLRequest(
            url: readerStartURL,
            cachePolicy: .useProtocolCachePolicy,
            timeoutInterval: 30
        )
        webView.load(request)
    }

    func prepareForNativeVoice() async throws {
        guard webView.url != nil else {
            throw NativeVoiceHandoffError.contextRelayUnavailable
        }
        let value = try await webView.callAsyncJavaScript(
            """
            const voice = window.RC && window.RC.computerVoice;
            if (!voice || typeof voice.isActive !== "function") {
              return "not-ready";
            }
            if (voice.isActive()) return "web-active";
            if (typeof voice.prepareNativeContextHandoff !== "function") {
              return "not-ready";
            }
            return await voice.prepareNativeContextHandoff();
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let result = value as? String ?? "not-ready"
        if result == "web-active" {
            throw NativeVoiceHandoffError.webVoiceAlreadyActive
        }
        if result != "native-ready" {
            throw NativeVoiceHandoffError.contextRelayUnavailable
        }
    }

    func updateNativeVoiceButton(state: NativeVoiceBridgeState) {
        guard webView.url != nil else {
            return
        }
        let detail = state.detail?.trimmingCharacters(in: .whitespacesAndNewlines)
        let title: String
        if let detail, !detail.isEmpty {
            title = "\(state.title)：\(detail)"
        } else {
            title = state.title
        }
        let value: [String: Any] = [
            "active": state.isActive,
            "busy": state.isBusy,
            "sessionId": state.sessionId ?? NSNull(),
            "title": title,
        ]
        guard
            JSONSerialization.isValidJSONObject(value),
            let data = try? JSONSerialization.data(withJSONObject: value),
            let literal = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView.evaluateJavaScript(
            "window.__bwNativeComputerVoiceApplyState?.(\(literal))"
        )
    }

    private func toggleNativeComputerVoice(
        appKind: DirectVoiceTargetApp
    ) {
        guard let bridge = nativeVoiceBridge else {
            return
        }
        Task { @MainActor [weak bridge] in
            guard let bridge else {
                return
            }
            switch bridge.state.phase {
            case .idle, .failed:
                await bridge.start(appKind: appKind)
            case .active, .suspended:
                await bridge.stop()
            case .preparing, .connecting, .starting, .stopping:
                return
            }
        }
    }

    private func handleNativeComputerContext(
        _ body: [String: Any]
    ) {
        guard
            let requestID = body["requestId"] as? String,
            directVoiceSafeID(requestID)
        else {
            return
        }
        guard
            body.count == 3,
            let action = body["action"] as? String,
            action == "context" || action == "active-reading",
            let rawFields = body["fields"] as? [String: Any],
            JSONSerialization.isValidJSONObject(rawFields),
            let data = try? JSONSerialization.data(withJSONObject: rawFields),
            let value = try? JSONDecoder().decode(
                DirectJSONValue.self,
                from: data
            ),
            let fields = value.objectValue
        else {
            replyNativeComputerContext(
                requestID: requestID,
                failure: DirectVoiceFailure(
                    code: "BW_NATIVE_COMPUTER_CONTEXT_SCHEMA",
                    message: "Reader 原生上下文请求格式无效",
                    retryable: false
                )
            )
            return
        }
        guard let bridge = nativeVoiceBridge else {
            replyNativeComputerContext(
                requestID: requestID,
                failure: DirectVoiceFailure(
                    code: "BW_NATIVE_COMPUTER_CONTEXT_INACTIVE",
                    message: "原生电脑语音会话未连接",
                    retryable: true
                )
            )
            return
        }
        Task { @MainActor [weak self, weak bridge] in
            guard let self, let bridge else { return }
            do {
                let result = try await bridge.requestReaderContext(
                    action: action,
                    fields: fields
                )
                self.replyNativeComputerContext(
                    requestID: requestID,
                    value: result
                )
            } catch {
                let failure = error as? DirectVoiceFailure
                    ?? DirectVoiceFailure(
                        code: "BW_NATIVE_COMPUTER_CONTEXT_FAILED",
                        message: error.localizedDescription,
                        retryable: true
                    )
                self.replyNativeComputerContext(
                    requestID: requestID,
                    failure: failure
                )
            }
        }
    }

    private func replyNativeComputerContext(
        requestID: String,
        value: DirectJSONValue? = nil,
        failure: DirectVoiceFailure? = nil
    ) {
        let payload: DirectJSONValue
        if let value, failure == nil {
            payload = .object([
                "requestId": .string(requestID),
                "ok": .bool(true),
                "value": value,
            ])
        } else {
            let resolved = failure ?? DirectVoiceFailure(
                code: "BW_NATIVE_COMPUTER_CONTEXT_FAILED",
                message: "Reader 原生上下文请求失败",
                retryable: true
            )
            payload = .object([
                "requestId": .string(requestID),
                "ok": .bool(false),
                "error": .object([
                    "code": .string(resolved.code),
                    "message": .string(resolved.message),
                    "retryable": .bool(resolved.retryable),
                ]),
            ])
        }
        guard
            let data = try? JSONEncoder().encode(payload),
            let literal = String(data: data, encoding: .utf8)
        else {
            return
        }
        webView.evaluateJavaScript(
            "window.__bwNativeComputerContextApplyResult?.(\(literal))"
        )
    }

}

extension ReaderWebViewModel {
    /// 把 toggle 被拒的原因回传给网页。
    ///
    /// 之前这条路径上的八个前置条件共用一个 `else { return }`,任何一条不满足都静默返回:
    /// 网页端只知道 postMessage 没抛异常,按钮不变色时无法区分是 App 没更新、URL 不匹配,
    /// 还是消息压根没到。这里只做上报,不改变任何控制流。
    fileprivate func reportNativeVoiceToggleRejected(_ info: [String: Any]) {
        var payload = info
        payload["appVersion"] = nativeAppBuildVersion
        guard
            let data = try? JSONSerialization.data(withJSONObject: payload),
            let json = String(data: data, encoding: .utf8)
        else {
            return
        }
        let script = """
        (() => {
          const info = \(json);
          window.__BW_NATIVE_COMPUTER_VOICE_LAST_REJECT__ = info;
          try {
            console.warn("[BWReader] native voice toggle rejected", info);
          } catch (error) {}
          try {
            window.dispatchEvent(new CustomEvent(
              "bw-native-computer-voice-reject",
              { detail: info }
            ));
          } catch (error) {}
          // iPad 上看不到 console,诊断必须直接显示在屏幕上,否则等于没加。
          // 只在被拒时出现,正常路径完全不触发。
          try {
            const failed = Object.keys(info).filter((key) => info[key] === false);
            const banner = document.createElement("div");
            banner.textContent = "语音按钮被 App 拒绝｜v" + info.appVersion +
              "｜未满足: " + (failed.length ? failed.join(", ") : "字段校验") +
              "｜count=" + info.bodyFieldCount +
              " action=" + info.action + " appKind=" + info.appKind;
            banner.setAttribute("style", [
              "position:fixed", "left:8px", "right:8px", "top:8px",
              "z-index:2147483647", "padding:10px 12px", "border-radius:10px",
              "background:rgba(176,0,32,.95)", "color:#fff",
              "font:13px/1.5 -apple-system,system-ui,sans-serif",
              "white-space:pre-wrap", "word-break:break-all",
              "box-shadow:0 2px 12px rgba(0,0,0,.35)"
            ].join(";"));
            banner.addEventListener("click", () => banner.remove());
            document.body.appendChild(banner);
            setTimeout(() => banner.remove(), 12000);
          } catch (error) {}
        })();
        """
        DispatchQueue.main.async { [weak self] in
            self?.webView.evaluateJavaScript(script, completionHandler: nil)
        }
    }
}

extension ReaderWebViewModel: WKScriptMessageHandler {
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        if message.name == nativeComputerVoiceMessageName {
            // 只读采样,不影响下面 guard 的判定;仅用于 guard 失败时说明是哪一条。
            let sampledBody = message.body as? [String: Any]
            let rejection: [String: Any] = [
                "isMainFrame": message.frameInfo.isMainFrame,
                "sameWebView": message.webView === webView,
                "schemeMatches": webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                "hostMatches": webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                "bodyIsDictionary": sampledBody != nil,
                "bodyFieldCount": sampledBody?.count ?? -1,
                "action": (sampledBody?["action"] as? String) ?? "<missing>",
                "appKind": (sampledBody?["appKind"] as? String) ?? "<missing>",
                "currentURL": webView.url?.absoluteString ?? "<nil>",
                "expectedURL": readerStartURL.absoluteString,
            ]
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                let body = message.body as? [String: Any],
                body.count == 2,
                body["action"] as? String == "toggle",
                let rawAppKind = body["appKind"] as? String,
                let appKind = DirectVoiceTargetApp(rawValue: rawAppKind)
            else {
                reportNativeVoiceToggleRejected(rejection)
                return
            }
            toggleNativeComputerVoice(appKind: appKind)
        } else if message.name == nativeComputerContextMessageName {
            guard
                message.frameInfo.isMainFrame,
                message.webView === webView,
                webView.url?.scheme?.lowercased()
                    == readerStartURL.scheme?.lowercased(),
                webView.url?.host?.lowercased()
                    == readerStartURL.host?.lowercased(),
                let body = message.body as? [String: Any]
            else {
                return
            }
            handleNativeComputerContext(body)
        }
    }
}

extension ReaderWebViewModel: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView,
        didStartProvisionalNavigation navigation: WKNavigation!
    ) {
        isLoading = true
        loadError = nil
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation!
    ) {
        isLoading = false
        loadError = nil
        if let nativeVoiceBridge {
            updateNativeVoiceButton(state: nativeVoiceBridge.state)
        }
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        recordLoadFailure(error)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        recordLoadFailure(error)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard
            let url = navigationAction.request.url,
            let scheme = url.scheme?.lowercased()
        else {
            decisionHandler(.allow)
            return
        }

        let webSchemes = ["http", "https", "about", "blob", "data"]
        guard !webSchemes.contains(scheme) else {
            decisionHandler(.allow)
            return
        }

        UIApplication.shared.open(url)
        decisionHandler(.cancel)
    }

    private func recordLoadFailure(_ error: Error) {
        let nsError = error as NSError
        guard nsError.code != NSURLErrorCancelled else {
            return
        }
        isLoading = false
        loadError = error.localizedDescription
    }
}

extension ReaderWebViewModel: WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            webView.load(URLRequest(url: url))
        }
        return nil
    }
}

struct ReaderWebView: UIViewRepresentable {
    @ObservedObject var model: ReaderWebViewModel

    func makeUIView(context: Context) -> WKWebView {
        model.loadIfNeeded()
        return model.webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {}
}
