// PDFReaderApp.swift —— bwicarus PDF 阅读器的 iOS 壳（Swift Playgrounds App 项目）
//
// 作用：全屏 WKWebView 指向网页阅读器，用**持久数据仓**——网页的 IndexedDB 整本缓存、登录
//      cookie 都存进 App 沙盒容器，**不受 Safari 7 天 ITP 清除 / 标签页配额驱逐** → 缓存真正长存、
//      只需登录一次。比「加到主屏 PWA」更稳，且能控生命周期（切走不重载、保留阅读位置）。
//
// 用法（iPad 上，无需 Mac）：
//   1. App Store 装「Swift Playgrounds」(免费)。
//   2. 打开 → 新建 → 选「App」。
//   3. 把这个文件的全部内容覆盖进自动生成的那个 .swift（或新建文件粘进来，删掉模板的 @main）。
//   4. 右上角 ▶ 运行 → 装到本机。第一次会要登录(输你的网站账号),之后保持登录。
//   注：免费 Apple ID 签的 App 约 7 天要回 Playgrounds 再跑一次重签；App 沙盒里缓存的 PDF 不会丢。
//
// 改地址只改下面 kStartURL 一行。

import SwiftUI
import WebKit

// ====== 配置：阅读器地址（Tailscale，证书有效，无需 ATS 例外）======
let kStartURL = URL(string: "https://bwicarus.taile44d0c.ts.net/pdf/")!

// ====== WKWebView 持有者（@StateObject 持有 → 整个 App 生命周期只建一次,切走/回来不重载）======
final class WebViewModel: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    let webView: WKWebView
    @Published var isLoading = false
    @Published var loadError: String? = nil

    override init() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()          // **持久仓**：IndexedDB/localStorage/cookie 存 App 容器
        cfg.allowsInlineMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        let prefs = WKWebpagePreferences()
        prefs.allowsContentJavaScript = true
        cfg.defaultWebpagePreferences = prefs
        webView = WKWebView(frame: .zero, configuration: cfg)
        super.init()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true   // 左缘右滑 = 后退（从书回列表）
        webView.allowsLinkPreview = false                     // 关长按链接预览（别跟阅读器的长按选词/词组冲突）
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.contentInsetAdjustmentBehavior = .never
    }

    func loadIfNeeded() {
        if webView.url == nil { reload() }
    }
    func reload() {
        loadError = nil
        webView.load(URLRequest(url: kStartURL, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 30))
    }

    // 进度
    func webView(_ w: WKWebView, didStartProvisionalNavigation n: WKNavigation!) { isLoading = true; loadError = nil }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) { isLoading = false }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { isLoading = false; loadError = e.localizedDescription }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        isLoading = false
        loadError = e.localizedDescription   // 离线且无缓存时会到这；连上网点「重试」即可
    }
    // target=_blank / window.open 在同一 webview 内打开（obsidian:// 等自定义 scheme 交给系统）
    func webView(_ w: WKWebView, createWebViewWith cfg: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { w.load(URLRequest(url: url)) }
        return nil
    }
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let scheme = url.scheme,
           !["http", "https", "about", "blob", "data"].contains(scheme.lowercased()) {
            UIApplication.shared.open(url)        // obsidian:// 等丢给系统
            decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }
}

struct WebContainer: UIViewRepresentable {
    @ObservedObject var model: WebViewModel
    func makeUIView(context: Context) -> WKWebView { model.loadIfNeeded(); return model.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

struct ContentView: View {
    @StateObject private var model = WebViewModel()
    var body: some View {
        ZStack(alignment: .top) {
            Color.black.ignoresSafeArea()
            WebContainer(model: model)
                .ignoresSafeArea(edges: .bottom)   // 顶部留给状态栏(别压住阅读器工具栏/☰),底部铺满
            if model.isLoading {
                ProgressView().tint(.white).scaleEffect(1.2).padding(10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.top, 8)
            }
            if let err = model.loadError {
                VStack(spacing: 14) {
                    Text("加载失败").font(.headline)
                    Text(err).font(.footnote).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Text("（确认 Tailscale 已连接）").font(.caption).foregroundStyle(.secondary)
                    Button("重试") { model.reload() }
                        .buttonStyle(.borderedProminent)
                }
                .padding(28)
                .frame(maxWidth: 360)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .preferredColorScheme(.dark)
        .statusBarHidden(false)
    }
}

@main
struct PDFReaderApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
