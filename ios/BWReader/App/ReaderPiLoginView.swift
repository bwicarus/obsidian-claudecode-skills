import SwiftUI
import WebKit

/// A separate, fixed-origin authentication surface. It shares the Reader
/// WKWebsiteDataStore but never replaces the local Reader shell with Pi PWA.
struct ReaderPiLoginView: View {
    @Environment(\.dismiss) private var dismiss
    let dataStore: WKWebsiteDataStore

    var body: some View {
        NavigationStack {
            ReaderPiLoginWebView(dataStore: dataStore)
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle("登录 Pi")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("完成") { dismiss() }
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

    func makeCoordinator() -> Coordinator { Coordinator() }

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

        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let url = navigationAction.request.url,
                  url.scheme?.lowercased() == "https",
                  url.host?.lowercased() == Self.allowedHost,
                  url.port == nil,
                  ["/login", "/logout", "/register", "/dashboard/"].contains(
                    url.path
                  ) else {
                decisionHandler(.cancel)
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
