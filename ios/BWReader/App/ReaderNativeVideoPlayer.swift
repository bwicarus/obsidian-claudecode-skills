import SwiftUI
import WebKit

/// Video is an explicit WebKit island. No reader shell, PDF canvas, message
/// history or book credentials are loaded into this view.
struct ReaderNativeVideo {
    let options: [String: Any]
    var title: String { options["title"] as? String ?? "视频" }
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        let source = value["src"] as? String ?? (id.hasPrefix("BV") || id.hasPrefix("av") ? "bili" : "yt")
        let pattern = source == "yt" ? "^[A-Za-z0-9_-]{11}$" : "^(BV[A-Za-z0-9]{10}|av[0-9]{1,16})$"
        guard ["yt", "bili"].contains(source), id.range(of: pattern, options: .regularExpression) != nil else { return nil }
        var result: [String: Any] = ["id": id, "src": source, "title": String((value["title"] as? String ?? "视频").prefix(240))]
        for key in ["start", "end", "rate"] {
            if let n = value[key] as? NSNumber, n.doubleValue.isFinite, n.doubleValue >= 0, n.doubleValue <= 100_000 { result[key] = n }
        }
        for key in ["loop", "cc"] { if let flag = value[key] as? Bool { result[key] = flag } }
        for key in ["noteId", "changeID", "removeID"] { if let text = value[key] as? String, text.utf8.count <= 256 { result[key] = text } }
        options = result
    }
}

@MainActor
struct ReaderNativeVideoButton: View {
    let video: ReaderNativeVideo
    @ObservedObject var model: ReaderNativeConversationModel
    @State private var playing = false
    var body: some View {
        Button { playing = true } label: {
            Label(video.title, systemImage: "play.rectangle.fill").font(.body).frame(maxWidth: .infinity, minHeight: 80)
        }.buttonStyle(.bordered)
            .sheet(isPresented: $playing) {
                ReaderNativeVideoPlayer(video: video, model: model, onClose: { playing = false })
            }
    }
}

@MainActor
struct ReaderNativeVideoPlayer: UIViewRepresentable {
    // YouTube's documented WebView identity: HTTPS + the installed bundle ID.
    static let originHost = (Bundle.main.bundleIdentifier ?? "space.bwicarus.bwreader2").lowercased()
    let video: ReaderNativeVideo
    let model: ReaderNativeConversationModel
    let onClose: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(model: model, scope: model.scope, changeID: video.options["changeID"] as? String,
        removeID: video.options["removeID"] as? String, onClose: onClose) }
    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.userContentController.addScriptMessageHandler(context.coordinator, contentWorld: .page, name: "readerVideo")
        let view = WKWebView(frame: .zero, configuration: config)
        view.isOpaque = false; view.backgroundColor = .black
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        context.coordinator.view = view
        do {
            guard let root = Bundle.main.resourceURL else { throw URLError(.fileDoesNotExist) }
            let script = try String(contentsOf: root.appendingPathComponent("ReaderBundle/static/pdf/rc-videoplayer.js"), encoding: .utf8)
            let data = try JSONSerialization.data(withJSONObject: video.options)
            let options = String(decoding: data, as: UTF8.self).replacingOccurrences(of: "<", with: "\\u003c")
            let html = Self.document(script: script, options: options)
            view.loadHTMLString(html, baseURL: URL(string: "https://" + Self.originHost + "/"))
        } catch {
            view.loadHTMLString("<meta name='viewport' content='width=device-width'><p>播放器资源无法读取，请关闭后重试。</p>", baseURL: nil)
        }
        return view
    }
    func updateUIView(_ view: WKWebView, context: Context) {
        if context.coordinator.scope != model.scope { context.coordinator.stop(); onClose() }
    }
    static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) { coordinator.stop() }

    static func document(script: String, options: String) -> String {
        """
        <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
        <meta name="referrer" content="strict-origin-when-cross-origin">
        <style>html,body{margin:0;background:#111;color:white;font:17px -apple-system;height:100%;overflow:hidden}</style>
        </head><body><script>
        window.RC={};window.__BW_NATIVE_LOCAL_READER__=true;window.__BW_NATIVE_VIDEO_ISLAND__=true;
        const host=webkit.messageHandlers.readerVideo;
        window.fetch=async function(path,init={}){
          const result=await host.postMessage({action:'fetch',path:String(path),method:init.method||'GET',body:init.body||''});
          return new Response(result.body,{status:result.status,headers:{'Content-Type':'application/json'}});
        };
        \(script.replacingOccurrences(of: "</script", with: "<\\/script"))
        RC.videoPlayer.open(Object.assign(\(options),{onClose:()=>host.postMessage({action:'close'}),
          onRemove:()=>host.postMessage({action:'remove'}),
          onChange:value=>host.postMessage({action:'change',value}).catch(error=>alert('保存失败：'+error))}));
        </script></body></html>
        """
    }

    final class Coordinator: NSObject, WKScriptMessageHandlerWithReply, WKNavigationDelegate, WKUIDelegate {
        let model: ReaderNativeConversationModel
        let scope: String
        let changeID: String?
        let removeID: String?
        let onClose: () -> Void
        weak var view: WKWebView?
        var stopped = false
        var tasks: [UUID: Task<Void, Never>] = [:]
        var backgroundObserver: NSObjectProtocol?
        init(model: ReaderNativeConversationModel, scope: String, changeID: String?, removeID: String?, onClose: @escaping () -> Void) {
            self.model = model; self.scope = scope; self.changeID = changeID; self.removeID = removeID; self.onClose = onClose
            super.init()
            backgroundObserver = NotificationCenter.default.addObserver(forName: UIApplication.didEnterBackgroundNotification,
                object: nil, queue: .main) { [weak self] _ in
                    Task { @MainActor in self?.stop(); self?.onClose() }
                }
        }
        func stop() {
            guard !stopped else { return }; stopped = true
            if let backgroundObserver { NotificationCenter.default.removeObserver(backgroundObserver) }; backgroundObserver = nil
            tasks.values.forEach { $0.cancel() }; tasks.removeAll()
            view?.stopLoading()
            view?.configuration.userContentController.removeScriptMessageHandler(forName: "readerVideo", contentWorld: .page)
            view?.loadHTMLString("", baseURL: nil)
            view?.navigationDelegate = nil; view?.uiDelegate = nil
        }
        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                                   replyHandler: @escaping (Any?, String?) -> Void) {
            guard !stopped, scope == model.scope, message.frameInfo.isMainFrame,
                  message.webView === view, message.frameInfo.securityOrigin.host == ReaderNativeVideoPlayer.originHost,
                  let input = message.body as? [String: Any] else { replyHandler(nil, "播放器已关闭"); return }
            if input["action"] as? String == "close" { replyHandler(true, nil); onClose(); return }
            if ["change", "remove"].contains(input["action"] as? String ?? "") {
                let removing = input["action"] as? String == "remove"
                guard let actionID = removing ? removeID : changeID else { replyHandler(true, nil); return }
                guard let handler = model.inspectionHandler else { replyHandler(nil, "视频保存不可用"); return }
                let value = input["value"] as? [String:Any] ?? [:]
                Task {
                    let result = await handler(["action":"liveAction","scope":scope,"actionId":actionID,"value":value])
                    if result["ok"] as? Bool == true { replyHandler(true,nil) }
                    else { replyHandler(nil,result["error"] as? String ?? "视频保存失败") }
                }
                return
            }
            guard input["action"] as? String == "fetch", let path = input["path"] as? String,
                  let method = input["method"] as? String, let body = input["body"] as? String,
                  Self.allowed(path: path, method: method), body.utf8.count <= 65_536 else {
                replyHandler(nil, "不支持的播放器请求"); return
            }
            let id = UUID()
            tasks[id] = Task { [weak self] in
                guard let self else { replyHandler(nil, "播放器已关闭"); return }
                defer { tasks.removeValue(forKey: id) }
                do {
                    let result = try await model.videoRequest(path: path, method: method, body: body)
                    guard !stopped, !Task.isCancelled, scope == model.scope else { throw CancellationError() }
                    replyHandler(result, nil)
                } catch { replyHandler(nil, error.localizedDescription) }
            }
        }
        static func allowed(path: String, method: String) -> Bool {
            if path == "/pdf/api/video-player-prefs" { return ["GET", "POST"].contains(method) }
            return method == "GET" && path.range(of: "^/pdf/api/video-subtitles/[A-Za-z0-9_-]{11}\\?source=(auto|hq)(&force=1)?$", options: .regularExpression) != nil
        }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { decisionHandler(.cancel); return }
            if action.targetFrame?.isMainFrame != false {
                if url.absoluteString == "about:blank" || url.host == ReaderNativeVideoPlayer.originHost { decisionHandler(.allow); return }
                if action.navigationType == .linkActivated && url.scheme == "https" { UIApplication.shared.open(url) }
                decisionHandler(.cancel); return
            }
            decisionHandler(["https", "about"].contains(url.scheme ?? "") ? .allow : .cancel)
        }
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url, url.scheme == "https", let host = url.host,
               ["youtube.com", "www.youtube.com", "youtu.be", "www.bilibili.com", "bilibili.com"].contains(host) { UIApplication.shared.open(url) }
            return nil
        }
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            if frame.isMainFrame { model.report(message) }
            completionHandler()
        }
    }
}
