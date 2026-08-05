import Foundation
import UIKit
import WebKit

enum NativeReaderCaptureError: LocalizedError {
    case pageUnavailable
    case invalidPagePayload
    case emptyViewport
    case snapshotUnavailable

    var errorDescription: String? {
        switch self {
        case .pageUnavailable:
            return "阅读器页面尚未加载完成"
        case .invalidPagePayload:
            return "无法读取当前页面信息"
        case .emptyViewport:
            return "当前阅读视口为空"
        case .snapshotUnavailable:
            return "无法截取当前阅读视口"
        }
    }
}

private struct NativeReaderJavaScriptSnapshot: Decodable {
    let title: String
    let url: String
    let file: String
    let page: String
    let pageCount: String
    let selection: String
    let visibleText: String
}

@MainActor
extension ReaderWebViewModel {
    @discardableResult
    func openNativeReaderURL(_ url: URL) -> Bool {
        guard
            url.user == nil,
            url.password == nil,
            url.scheme?.lowercased() == "https",
            url.host?.lowercased() == "bwicarus.taile44d0c.ts.net"
        else {
            return false
        }
        webView.load(
            URLRequest(
                url: url,
                cachePolicy: .useProtocolCachePolicy,
                timeoutInterval: 30
            )
        )
        return true
    }

    /// Captures a bounded, local snapshot of the page currently visible in the
    /// App's WKWebView. This is for App-native tools and the App Group cache;
    /// it does not participate in the Windows voice/context bridge.
    func captureNativeReaderSnapshot() async throws -> ReaderSharedSnapshot {
        guard webView.url != nil else {
            throw NativeReaderCaptureError.pageUnavailable
        }

        let script = #"""
        (() => {
          const clean = (value, limit) => String(value == null ? "" : value)
            .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
            .slice(0, limit);
          let state = null;
          try {
            state = window.RC && RC.ctxSync && typeof RC.ctxSync._state === "function"
              ? RC.ctxSync._state()
              : null;
          } catch (_) {}
          const pending = state && state.pend && typeof state.pend === "object"
            ? state.pend : {};
          const canonical = state && state.canonical && typeof state.canonical === "object"
            ? state.canonical : null;
          const pendingFile = pending.kind === "web" ? pending.url : pending.file;
          const viewBound = canonical && canonical.viewFile === pendingFile &&
            String(canonical.viewPage == null ? "" : canonical.viewPage) ===
              String(pending.pos == null ? "" : pending.pos);

          let selected = "";
          try { selected = window.getSelection ? window.getSelection().toString() : ""; }
          catch (_) {}
          if (!selected && typeof pending.selection === "string") {
            selected = pending.selection;
          }

          // Read text nodes that actually intersect the visible viewport. The
          // bounded walk avoids turning a long book or an infinite web page
          // into a costly full-DOM export.
          const pieces = [];
          let count = 0;
          let length = 0;
          try {
            const root = document.body || document.documentElement;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode()) && count < 1800 && length < 18000) {
              count += 1;
              const parent = node.parentElement;
              if (!parent || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(parent.tagName)) continue;
              const value = String(node.nodeValue || "").replace(/\s+/g, " ").trim();
              if (!value) continue;
              const style = getComputedStyle(parent);
              if (style.display === "none" || style.visibility === "hidden" ||
                  Number(style.opacity || "1") === 0) continue;
              const range = document.createRange();
              range.selectNodeContents(node);
              const rects = Array.from(range.getClientRects());
              const visible = rects.some((rect) =>
                rect.width > 0 && rect.height > 0 && rect.bottom >= 0 && rect.right >= 0 &&
                rect.top <= innerHeight && rect.left <= innerWidth
              );
              if (!visible) continue;
              pieces.push(value);
              length += value.length + 1;
            }
          } catch (_) {}
          let visibleText = pieces.join("\n");
          if (!visibleText) {
            try { visibleText = (document.body && document.body.innerText) || ""; }
            catch (_) {}
          }

          return JSON.stringify({
            title: clean(pending.title || document.title, 1024),
            url: clean(location.href, 2048),
            file: clean(viewBound ? canonical.file : (pendingFile || ""), 2048),
            page: clean(viewBound ? canonical.page : pending.pos, 64),
            pageCount: clean(pending.total, 64),
            selection: clean(selected, 4096),
            visibleText: clean(visibleText, 16384)
          });
        })()
        """#

        let raw: Any = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Any, Error>) in
            webView.evaluateJavaScript(script) { value, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let value {
                    continuation.resume(returning: value)
                } else {
                    continuation.resume(
                        throwing: NativeReaderCaptureError.invalidPagePayload
                    )
                }
            }
        }
        guard
            let json = raw as? String,
            let data = json.data(using: .utf8),
            let value = try? JSONDecoder().decode(
                NativeReaderJavaScriptSnapshot.self,
                from: data
            )
        else {
            throw NativeReaderCaptureError.invalidPagePayload
        }
        return ReaderSharedSnapshot(
            title: value.title,
            url: value.url,
            file: value.file,
            page: value.page,
            pageCount: value.pageCount,
            selection: value.selection,
            visibleText: value.visibleText
        )
    }

    /// Takes a pixel-accurate image of only the currently visible WKWebView.
    /// It intentionally does not capture the full scrollable document.
    func captureNativeViewportImage() async throws -> UIImage {
        guard webView.url != nil else {
            throw NativeReaderCaptureError.pageUnavailable
        }
        guard !webView.bounds.isEmpty else {
            throw NativeReaderCaptureError.emptyViewport
        }
        let configuration = WKSnapshotConfiguration()
        configuration.rect = webView.bounds
        let image: UIImage = try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<UIImage, Error>) in
            webView.takeSnapshot(with: configuration) { image, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if let image {
                    continuation.resume(returning: image)
                } else {
                    continuation.resume(
                        throwing: NativeReaderCaptureError.snapshotUnavailable
                    )
                }
            }
        }
        return image
    }

    @discardableResult
    func refreshNativeSharedSnapshot() async throws -> ReaderSharedSnapshot {
        let snapshot = try await captureNativeReaderSnapshot()
        try ReaderNativeFeatureStore().writeSnapshot(snapshot)
        return snapshot
    }
}
