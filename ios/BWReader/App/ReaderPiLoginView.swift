import SwiftUI
import WebKit

/// A separate, fixed-origin authentication surface. It shares the Reader
/// WKWebsiteDataStore but never replaces the local Reader shell with Pi PWA.
struct ReaderPiLoginView: View {
    @Environment(\.dismiss) private var dismiss
    let dataStore: WKWebsiteDataStore
    @State private var signedIn = false

    var body: some View {
        NavigationStack {
            ReaderPiLoginWebView(dataStore: dataStore) { signedIn = true }
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle("登录 Pi")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("完成") { dismiss() }
                    }
                }
                .overlay(alignment: .center) {
                    // ⚠ 成功必须**看得见**。这一条就是这次 bug 的全部修法:
                    // 服务端早就成功了,而界面什么都没说。
                    if signedIn {
                        VStack(spacing: 10) {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 44))
                                .foregroundStyle(Color.green)
                            Text("已登录 Pi").font(.headline)
                            Text("点右上角「完成」，再执行「与 Pi 同步」")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                        }
                        .padding(24)
                        .background(.ultraThinMaterial,
                                    in: RoundedRectangle(cornerRadius: 16))
                        .padding(24)
                    }
                }
                .safeAreaInset(edge: .bottom) {
                    Text("登录完成后点“完成”，再重新执行“与 Pi 同步”。登录凭据只保存在 App 的网站数据存储中，不会交给书页。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 10)
                        .frame(maxWidth: .infinity)
                        .background(.ultraThinMaterial)
                }
        }
    }
}

private struct ReaderPiLoginWebView: UIViewRepresentable {
    let dataStore: WKWebsiteDataStore
    let onSuccess: () -> Void

    func makeCoordinator() -> Coordinator {
        let coordinator = Coordinator()
        coordinator.onSuccess = onSuccess
        return coordinator
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.load(URLRequest(
            url: ReaderNativePiSyncBridge.loginURL,
            cachePolicy: .reloadIgnoringLocalCacheData
        ))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        private static let allowedHost = "bwicarus.taile44d0c.ts.net"
        /// 属于登录流程本身的路径。跳到**这之外**就意味着已经通过了登录。
        private static let loginFlowPaths: Set<String> = [
            "/login", "/logout", "/register",
        ]
        var onSuccess: () -> Void = {}

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url,
                  url.scheme?.lowercased() == "https",
                  url.host?.lowercased() == Self.allowedHost,
                  url.port == nil else {
                decisionHandler(.cancel)
                return
            }
            // ⚠ **登录成功的唯一信号,就是它想跳到登录流程之外的地方。**
            //
            // 服务端登录成功后会 302 到 `next`(实测是 /pdf/api/library/catalog)。
            // 原来的白名单不含它 → 直接 .cancel **一声不吭** → 登录页原封不动
            // → 跟失败长得一模一样。
            //
            // 用户 2026-08-28 因此连点了好几次:第二次 CSRF 令牌已被消费得到
            // 「会话已过期」,第三次重输时真打错了 —— 而服务端日志显示
            // **第一次就已经 302 成功了**。
            //
            // 取消那次跳转仍然是对的(不该在登录页里加载 API 返回),
            // 但它**必须出声**。
            if !Self.loginFlowPaths.contains(url.path) {
                decisionHandler(.cancel)
                onSuccess()
                return
            }
            decisionHandler(.allow)
        }

        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            // Authentication must stay in this fixed-origin sheet.
            nil
        }
    }
}
